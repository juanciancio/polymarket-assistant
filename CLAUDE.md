# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
pip install -r requirements.txt
python main.py
```

No build step, no tests. The app is a live terminal dashboard — it prompts for coin and timeframe, then streams data until killed with Ctrl+C.

## Architecture

The app is fully async (`asyncio`). `main.py` is the entry point: it prompts the user for a coin and timeframe, bootstraps historical candles, then runs four coroutines concurrently via `asyncio.gather`:

- `feeds.ob_poller` — polls Binance REST every 2s for the order book
- `feeds.binance_feed` — WebSocket stream for live trades and kline candles
- `feeds.pm_feed` — WebSocket stream for Polymarket Up/Down contract prices
- `display_loop` (in `main.py`) — redraws the dashboard every 10s (3s for 5m timeframe)

### Shared state: `feeds.State`

All four coroutines share a single `feeds.State` instance (a plain dataclass-style object). There is no locking — updates are small and Python's GIL makes this safe enough for this use case.

Fields:

- `bids`, `asks` — current order book (list of `(price, qty)` tuples)
- `mid` — midpoint price
- `trades` — rolling list of recent trades (pruned to last 10 min / 5000 entries)
- `klines`, `cur_kline` — historical + live-updating candles
- `pm_up_id`, `pm_dn_id` — Polymarket token IDs for Up/Down contracts
- `pm_up`, `pm_dn` — current best ask prices for those contracts

### Module responsibilities

- **`src/config.py`** — single source of truth for all constants: coins, Binance symbols, Polymarket slugs, timeframe mappings, indicator parameters (`RSI_PERIOD`, `MACD_FAST/SLOW/SIG`, `EMA_S/L`, `OBI_THRESH`, `BIAS_WEIGHTS`, etc.) and display settings. Change constants here, not inline.

- **`src/feeds.py`** — data acquisition only. Contains `State`, WebSocket/REST feed coroutines, and the Polymarket slug-building logic (`_build_slug`). The slug format varies by timeframe (5m/15m use Unix timestamps, 4h uses epoch-aligned timestamps, 1h/daily use Eastern Time human-readable slugs).

- **`src/indicators.py`** — pure functions, no I/O. Takes `bids`, `asks`, `mid`, `trades`, `klines` as arguments and returns computed values. Contains: `obi`, `walls`, `depth_usd`, `cvd`, `vol_profile`, `rsi`, `macd`, `vwap`, `emas`, `heikin_ashi`, and `bias_score`.

- **`src/dashboard.py`** — rendering only, uses [Rich](https://github.com/Textualize/rich). Calls indicator functions directly and builds panels: ORDER BOOK, FLOW & VOLUME, TECHNICAL, SIGNALS, and a header. The `render()` function returns a `_Group` consumed by `rich.live.Live`.

### Three scoring layers

The dashboard computes three parallel scores:

1. **TREND** (`_score_trend` in `dashboard.py`) — qualitative integer score, thresholded at ±3 for BULLISH/BEARISH/NEUTRAL label.
2. **BIAS** (`bias_score` in `indicators.py`) — weighted float in [-100, +100], normalized by `sum(BIAS_WEIGHTS.values()) = 56`. Weights are defined in `config.BIAS_WEIGHTS`.
3. **ENTRY** (`calculate_entry_score` in `src/scoring.py`) — integer score with specific bullish/bearish conditions (+2/+1/-1/-2 per condition). Fires an entry signal when `|score| >= 4`. Alerts are logged to `signals_log.json` (JSONL format, one JSON object per line) only on state transitions (NEUTRAL→signal or direction flip).

### Kline timeframe mapping

The selected UI timeframe (5m, 15m, 1h, 4h, daily) maps to a Binance kline interval via `config.TF_KLINE` (e.g., "1h" → "1m" candles). Polymarket markets use a separate slug format per timeframe resolved by `_build_slug` in Eastern Time.

## System Context (Live Trading)

### Mission

Operate a live trading system on Polymarket that generates the highest
number of effective trades per day with real capital, minimizing losses
from bugs, orphaned positions, and excessive slippage.

The goal is not just profitability — it is **system stability**.
Every unresolved bug can cost real capital within seconds.

---

### How trading works

Polymarket offers binary Up/Down contracts per coin and timeframe
(BTC/ETH/SOL/XRP × 5m/15m/1h/4h/daily). The contract price ranges
from 0 to 1 and resolves at 0 or 1 at expiry.

```
LONG  → buys UP contract   → wins if price goes up
SHORT → buys DOWN contract → wins if price goes down
        (DOWN price = 1 - pm_up)
```

The system detects MAX_BULLISH / MAX_BEARISH signals with score ≥ ±5
combining 11 indicators (OBI, CVD, VWAP, EMA, HA, MACD, RSI, etc.)
and executes FOK (Fill or Kill) orders via `py-clob-client`.

---

### Current system parameters

#### Entry (live_trader.py)

```
Entry ranges:
  LONG:  0.55 ≤ detected_price < 0.75
  SHORT: 0.30 ≤ detected_price < 0.70

Capital: MAX_POSITION_SIZE (from .env, default $5) — always 1×
  No volatility multipliers (deliberate decision, see incidents below)

Volatility tiers (display and min_secs only, do NOT affect capital):
  ULTRA STABLE  (<0.10%):    min_secs=30
  VERY STABLE   (0.10-0.20%): min_secs=40
  NORMAL        (0.20-0.50%): min_secs=60
  VOLATILE      (>=0.50%):    min_secs=90
```

#### TP / SL calculation

```python
tp_target = math.floor((actual_fill_price + 0.09) * 100) / 100
# floor because Polymarket WebSocket reports exactly 2 decimal places

# SL depends on fill price:
if fill >= 0.72 (TRAIL_ARMED):
    sl = math.ceil((fill - 0.08) * 100) / 100
elif capital > max_position_size:
    sl = math.ceil((fill - 0.10) * 100) / 100
else:
    sl = math.ceil((fill - 0.15) * 100) / 100
# ceil so SL is a reportable price and triggers before falling through

TRAIL_ARMED = 0.72
TRAIL_DROP  = 0.12
```

#### Guards in execute_signal (ORDER IS LOAD-BEARING)

```
1.  live_trading check
2.  client check
3.  pm_reconnecting / reconnection_in_progress
4.  last_token_switch < 10.0s (stale price after contract switch)
5.  circuit breaker (_circuit_open_until)
6.  current_open_position (one position at a time)
7.  CLOSE_FAILED active (if token still active → block)
8.  daily loss limit
9.  cooldown (10s)
10. burst limit (5/5min)
11. direction + VWAP filter
12. volatility tier + range check (< upper bound)
13. capital = max_position_size (always 1×)
14. expiry guard (adaptive min_secs)
15. _opening_position = True  ← MUST be before first await
16. token_id + _recent_losses check (inside lock)
17. liquidity check (asks side)
18. place_buy_order ← real money from here
19. [try block] fill price, slippage guards, SL/TP, position save
```

#### Post-buy guards (inside try block)

```
1. Absurd contracts (> capital/0.10)          → _persistent_emergency_sell
2. Fill out of safe range (< 0.05 or > 0.95)  → return None
3. Fill-range: fill > long_upper or short_upper → _persistent_emergency_sell
4. Large negative slippage (< -5%)             → _persistent_emergency_sell
5. Large positive slippage (> +5%)             → warn, track with real fill
```

#### Capital protection

```python
# Circuit breaker
3 consecutive losses → 5 minute pause

# Recent losses per contract+direction
3 losses in same contract+direction → block until new contract
TTL: 300s, cleanup: 600s

# Daily loss limit
daily_pnl <= -daily_loss_limit → stop all trading

# Cooldown
10s between trades (last_close_timestamp must be set on ALL close paths)
```

---

### Emergency sell system

**`_persistent_emergency_sell()`** — background task launched with
`asyncio.create_task()`. Never blocks the main event loop.

```
Attempt 1 (0.99×) → fail → wait 5s
Attempt 2 (0.97×) → fail → wait 5s
Attempt 3 (0.95×) → fail → wait 15s
Attempt 4 (0.90×) → fail → wait 15s
Attempt 5 (0.85×) → fail → wait 15s
Attempt 6 (0.80×) → fail → wait 15s
...

Each cycle: has the contract expired?
  YES → _save_close_failed() → task ends
  NO  → next attempt
```

On success: saves to `live_trades.json` with real PnL.
On contract expiry: saves `CLOSE_FAILED`.

**`recover_close_failed()`** — called on startup. For each
`CLOSE_FAILED` found in the JSON:

- Token still active → launch background sell task
- Token expired → register as LOSS_SL with pnl=-capital

---

### Historical incidents (do not repeat)

#### Incident 1 — 7 orphaned buys ($40 lost)

`_TRAIL_ARMED` without `self.` → NameError post-buy → position never
saved → `_opening_position` released → 7 consecutive buys until
balance was zero.
**Fix:** `self._TRAIL_ARMED` + try/except emergency sell wrapper.

#### Incident 2 — Double buy race condition

Signal fired during await → `_opening_position` not set yet → two
concurrent buys executed.
**Fix:** `_opening_position = True` set BEFORE the first await.

#### Incident 3 — Stale price after contract switch (-$4)

`pm_up` held old contract price ~200ms after switch → detected=0.79
but new contract at 0.41 → -48% slippage.
**Fix:** `last_token_switch < 10s` guard.

#### Incident 4 — "not enough balance" cascade (20 attempts)

`CLOSE_FAILED` saved without setting `last_close_timestamp` →
cooldown never activated → signals kept firing → balance depleted.
**Fix:** `last_close_timestamp` in `_save_close_failed()` + smart
CLOSE_FAILED guard (only blocks if token is still active).

#### Incident 5 — Recovery task on expired token

On restart with CLOSE_FAILED from expired contract →
`_try_sell_once` returned "No orderbook exists" in infinite loop.
**Fix:** `recover_close_failed()` checks if token is in
`active_tokens` before launching the task.

#### Incident 6 — BTC contract gap -$7.05

ULTRA STABLE (spot 0.042%) → capital $15 (3× multiplier) →
Polymarket contract gapped 0.17 in one tick → SL skipped →
sold at 0.39.
**Fix:** multipliers removed, capital always 1×.

#### Incident 7 — TP not triggering on SHORT positions

`1.0 - 0.16 = 0.8399999...` < `tp_target=0.84` → IEEE 754 float
subtraction error. Price hit TP exactly but comparison failed.
**Fix:** `current = round(1.0 - pm_up, 2)` in `check_and_close()`.

#### Incident 8 — 4-decimal TP never reached

`tp_target = round(fill + 0.09, 4) = 0.7677` — Polymarket only
reports 2 decimal prices. `0.76 >= 0.7677` → False. Trade went
to SL instead of TP.
**Fix:** `math.floor((fill + 0.09) * 100) / 100`.

---

### Known active issues

#### Systematically high entry slippage

```
errors_log.json consistently shows:
+5.42%  +5.19%  +8.16%  +14.14%  +7.14%  +8.82%  +7.26%
```

`pm_up` from WebSocket = best ask (top of book).
Capital $5-15 consumes multiple levels → real fill always worse.
**No fix applied yet.**

Possible approaches:

- Verify ask depth before entry and estimate real fill price
- Only enter if estimated fill slippage < 3%
- Reduce capital so it fits within the best ask level

#### Frequent FOK cancellations

```
errors_log.json: multiple "order couldn't be fully filled"
```

Happens during low liquidity periods. Not a bug — market behavior.
Burst limit partially filters re-entries but doesn't eliminate them.

---

### Current metrics

```
Win Rate: 47.7%  (target: >60%)
PnL:      -$13.37
Avg slip:  +1.77%
```

Low win rate likely caused by:

1. High slippage moves real fill far from detected price → SL closer
2. Entry ranges possibly too wide (entering at unfavorable prices)
3. Signal has edge but execution destroys it

---

### Critical safety rules (never violate)

```
NEVER:
- Reference class constants without self. inside methods
  (_TRAIL_ARMED, _COOLDOWN_SECS, etc.)
- Add code between place_buy_order() and self._open_position = position
  without try/except + emergency sell
- Move _opening_position = False outside the finally block
- Add O(n) or I/O in the "new_status is None" branch of check_and_close()
- Remove or reorder guards 1-15 in execute_signal()
- Add inline volatility comparisons — always use self._volatility_tier()

ALWAYS:
- Emergency sell pattern after any executed buy
- self._TRAIL_ARMED (with self.)
- math.floor for tp_target, math.ceil for sl_target
- round(current, 2) in check_and_close() before any TP/SL comparison
- last_close_timestamp set on ALL close/emergency paths
- asyncio.create_task() for _persistent_emergency_sell (never await inline)
```

---

### Key files

```
main.py              — entry point, asyncio.gather of all coroutines
src/live_trader.py   — all live trading logic
src/paper_trading.py — simulated trading (runs in parallel)
src/feeds.py         — Binance and Polymarket data feeds
src/indicators.py    — pure indicator functions (no I/O)
src/scoring.py       — entry score (MAX_BULLISH/MAX_BEARISH at ±5)
src/dashboard.py     — Rich terminal dashboard
src/config.py        — global constants
live_trades.json     — live trade history (source of truth)
errors_log.json      — runtime errors (JSONL)
signals_log.json     — detected signals (JSONL, max 500 entries)
.env                 — LIVE_TRADING, MAX_POSITION_SIZE, MAX_DAILY_LOSS
```

---

### How to diagnose a problem

1. **Read `errors_log.json`** — identify method and timestamp
2. **Cross with `live_trades.json`** — find the affected position
3. **Find the timestamp in terminal logs** — reconstruct the flow
4. **Ask: is this a code bug or market behavior?**
   - Code bug → surgical fix
   - Market behavior → parameter adjustment or new logic

When writing fixes:

- Always show exact BEFORE/AFTER of the code being changed
- Never modify more than necessary in a single prompt
- After each fix, verify with real logs on the next run
- The system operates with **real capital** — every bug has immediate cost

---

### Priority next steps

1. **Investigate systematically high slippage** — this is the main
   factor destroying win rate. Real fill is consistently 5-14% above
   detected price.

2. **Validate win rate with 50+ clean trades** post all stability
   fixes applied.

3. **Analyze if score threshold ±5 is correct** — consider raising
   to ±6 to filter weak signals that become losses.

4. **Add liquidity filter on entry** — if the ask book doesn't cover
   `capital / detected_price` contracts within 3% slippage, skip entry.

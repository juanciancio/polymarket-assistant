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

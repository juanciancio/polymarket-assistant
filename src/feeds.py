import asyncio
import json
import random
import time

import requests
import websockets
from datetime import datetime, timezone, timedelta

import config


class State:
    def __init__(self):
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        self.mid: float = 0.0

        self.trades: list[dict] = []

        self.klines: list[dict] = []
        self.cur_kline: dict | None = None

        self.pm_up_id:  str | None = None
        self.pm_dn_id:  str | None = None
        self.pm_up:     float | None = None
        self.pm_dn:     float | None = None

        # ── PM connection health ─────────────────────────────────
        self.last_pm_update: float    = 0.0   # epoch of last WS message
        self.pm_price_frozen_count: int = 0   # consecutive stale-price ticks
        self.pm_needs_reconnect: bool  = False # watchdog/scheduler → pm_feed signal
        self.pm_reconnecting:    bool  = False # True while searching new contract

        # ── Proactive reconnection (endDate-based) ────────────────
        self.market_end_time: "datetime | None"  = None  # endDate of current contract
        self.next_token_up:   str | None         = None  # pre-fetched next Up token
        self.next_token_dn:   str | None         = None  # pre-fetched next Down token
        self.next_token_prefetched: bool         = False # True once prefetch attempted
        self.reconnection_in_progress: bool      = False # guard: only one actor reconnects
        self.last_token_switch:        float     = 0.0   # epoch of last token swap


OB_POLL_INTERVAL = 2


async def ob_poller(symbol: str, state: State):
    url    = f"{config.BINANCE_REST}/depth"
    params = {"symbol": symbol, "limit": 20}
    print(f"  [Binance OB] polling {symbol} every {OB_POLL_INTERVAL}s")
    while True:
        try:
            resp = await asyncio.to_thread(
                lambda: requests.get(url, params=params, timeout=3).json()
            )
            state.bids = [(float(p), float(q)) for p, q in resp["bids"]]
            state.asks = [(float(p), float(q)) for p, q in resp["asks"]]
            if state.bids and state.asks:
                state.mid = (state.bids[0][0] + state.asks[0][0]) / 2
        except Exception:
            pass
        await asyncio.sleep(OB_POLL_INTERVAL)


async def binance_feed(symbol: str, kline_iv: str, state: State):
    sym = symbol.lower()
    streams = "/".join([
        f"{sym}@trade",
        f"{sym}@kline_{kline_iv}",
    ])
    url = f"{config.BINANCE_WS}?streams={streams}"

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=60,
                close_timeout=10
            ) as ws:
                print(f"  [Binance WS] connected – {symbol}")

                while True:
                    try:
                        data   = json.loads(await ws.recv())
                        stream = data.get("stream", "")
                        pay    = data["data"]

                        if "@trade" in stream:
                            state.trades.append({
                                "t":      pay["T"] / 1000.0,
                                "price":  float(pay["p"]),
                                "qty":    float(pay["q"]),
                                "is_buy": not pay["m"],
                            })
                            if len(state.trades) > 5000:
                                cut = time.time() - config.TRADE_TTL
                                state.trades = [t for t in state.trades if t["t"] >= cut]

                        elif "@kline" in stream:
                            k = pay["k"]
                            candle = {
                                "t": k["t"] / 1000.0,
                                "o": float(k["o"]), "h": float(k["h"]),
                                "l": float(k["l"]), "c": float(k["c"]),
                                "v": float(k["v"]),
                            }
                            state.cur_kline = candle
                            if k["x"]:
                                state.klines.append(candle)
                                state.klines = state.klines[-config.KLINE_MAX:]

                    except websockets.exceptions.ConnectionClosed:
                        print(f"  [Binance WS] connection closed, reconnecting...")
                        break

        except Exception as e:
            print(f"  [Binance WS] connection error: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


async def bootstrap(symbol: str, interval: str, state: State):
    resp = await asyncio.to_thread(
        lambda: requests.get(
            f"{config.BINANCE_REST}/klines",
            params={"symbol": symbol, "interval": interval, "limit": config.KLINE_BOOT},
        ).json()
    )
    state.klines = [
        {
            "t": r[0] / 1e3,
            "o": float(r[1]), "h": float(r[2]),
            "l": float(r[3]), "c": float(r[4]),
            "v": float(r[5]),
        }
        for r in resp
    ]
    print(f"  [Binance] loaded {len(state.klines)} historical candles")


_MONTHS = ["", "january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]


def _et_now() -> datetime:
    utc = datetime.now(timezone.utc)
    year = utc.year

    mar1_dow  = datetime(year, 3, 1).weekday()
    mar_sun   = 1 + (6 - mar1_dow) % 7
    dst_start = datetime(year, 3, mar_sun + 7, 2, 0, 0, tzinfo=timezone.utc)

    nov1_dow = datetime(year, 11, 1).weekday()
    nov_sun  = 1 + (6 - nov1_dow) % 7
    dst_end  = datetime(year, 11, nov_sun, 6, 0, 0, tzinfo=timezone.utc)

    offset = timedelta(hours=-4) if dst_start <= utc < dst_end else timedelta(hours=-5)
    return utc + offset


def _to_12h(hour24: int) -> str:
    if hour24 == 0:
        return "12am"
    if hour24 < 12:
        return f"{hour24}am"
    if hour24 == 12:
        return "12pm"
    return f"{hour24 - 12}pm"


def _build_slug(coin: str, tf: str) -> str | None:
    now_utc = datetime.now(timezone.utc)
    now_ts  = int(now_utc.timestamp())
    et      = _et_now()

    if tf == "5m":
        ts = (now_ts // 300) * 300
        return f"{config.COIN_PM[coin]}-updown-5m-{ts}"

    if tf == "15m":
        ts = (now_ts // 900) * 900
        return f"{config.COIN_PM[coin]}-updown-15m-{ts}"

    if tf == "4h":
        ts = ((now_ts - 3600) // 14400) * 14400 + 3600
        return f"{config.COIN_PM[coin]}-updown-4h-{ts}"

    if tf == "1h":
        return (f"{config.COIN_PM_LONG[coin]}-up-or-down-"
                f"{_MONTHS[et.month]}-{et.day}-{_to_12h(et.hour)}-et")

    if tf == "daily":
        resolution = et.replace(hour=12, minute=0, second=0, microsecond=0)
        target      = et if et < resolution else et + timedelta(days=1)
        return (f"{config.COIN_PM_LONG[coin]}-up-or-down-on-"
                f"{_MONTHS[target.month]}-{target.day}")

    return None


def fetch_pm_event_data(coin: str, tf: str) -> dict | None:
    """Fetch full event data from Polymarket API (blocking — call via asyncio.to_thread in async contexts)."""
    slug = _build_slug(coin, tf)
    if slug is None:
        return None
    try:
        data = requests.get(config.PM_GAMMA, params={"slug": slug, "limit": 1}, timeout=5).json()
        if not data or data[0].get("ticker") != slug:
            print(f"  [PM] no active market for slug: {slug}")
            return None
        return data[0]
    except Exception as e:
        print(f"  [PM] event fetch failed ({slug}): {e}")
        return None


async def fetch_pm_tokens_full_async(coin: str, tf: str) -> tuple:
    """Async wrapper for fetch_pm_tokens_full — non-blocking."""
    return await asyncio.to_thread(fetch_pm_tokens_full, coin, tf)


async def prefetch_next_pm_tokens_async(coin: str, tf: str) -> tuple:
    """Async wrapper for prefetch_next_pm_tokens — non-blocking."""
    return await asyncio.to_thread(prefetch_next_pm_tokens, coin, tf)


def fetch_pm_tokens(coin: str, tf: str) -> tuple:
    """Fetch PM token IDs for up/down markets."""
    event_data = fetch_pm_event_data(coin, tf)
    if event_data is None:
        return None, None
    try:
        ids = json.loads(event_data["markets"][0]["clobTokenIds"])
        return ids[0], ids[1]
    except Exception as e:
        print(f"  [PM] token extraction failed: {e}")
        return None, None


def fetch_pm_tokens_full(coin: str, tf: str) -> tuple:
    """Fetch PM token IDs plus endDate for the current contract.

    Returns:
        (up_id, dn_id, end_time) where end_time is a timezone-aware datetime or None.
    """
    event_data = fetch_pm_event_data(coin, tf)
    if event_data is None:
        return None, None, None
    try:
        ids      = json.loads(event_data["markets"][0]["clobTokenIds"])
        up_id    = ids[0]
        dn_id    = ids[1]
        end_raw  = event_data.get("endDate") or event_data.get("markets", [{}])[0].get("endDate")
        end_time = None
        if end_raw:
            # Gamma returns ISO-8601 strings like "2025-03-11T14:00:00Z"
            end_time = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        return up_id, dn_id, end_time
    except Exception as e:
        print(f"  [PM] token/endDate extraction failed: {e}")
        return None, None, None


def _build_next_slug(coin: str, tf: str) -> str | None:
    """Build the slug for the NEXT contract period (for timestamp-based TFs only)."""
    now_utc = datetime.now(timezone.utc)
    now_ts  = int(now_utc.timestamp())

    if tf == "5m":
        ts = (now_ts // 300) * 300 + 300
        return f"{config.COIN_PM[coin]}-updown-5m-{ts}"

    if tf == "15m":
        ts = (now_ts // 900) * 900 + 900
        return f"{config.COIN_PM[coin]}-updown-15m-{ts}"

    if tf == "4h":
        ts = ((now_ts - 3600) // 14400) * 14400 + 3600 + 14400
        return f"{config.COIN_PM[coin]}-updown-4h-{ts}"

    # 1h / daily slugs are ET-time-based; caller should fall back to T+0 re-fetch
    return None


def prefetch_next_pm_tokens(coin: str, tf: str) -> tuple:
    """Try to fetch the next contract's tokens before the current one resolves.

    Returns:
        (up_id, dn_id) or (None, None) if not yet available.
    """
    slug = _build_next_slug(coin, tf)
    if slug is None:
        return None, None
    try:
        data = requests.get(config.PM_GAMMA, params={"slug": slug, "limit": 1}, timeout=5).json()
        if not data or data[0].get("ticker") != slug:
            return None, None
        ids = json.loads(data[0]["markets"][0]["clobTokenIds"])
        print(f"  [PM scheduler] next contract pre-fetched: {ids[0][:24]}…")
        return ids[0], ids[1]
    except Exception as e:
        print(f"  [PM scheduler] prefetch failed ({slug}): {e}")
        return None, None


def apply_new_pm_tokens(state: State, new_up: str, new_dn: str):
    """Swap token IDs in state and signal pm_feed to reconnect."""
    state.pm_up_id              = new_up
    state.pm_dn_id              = new_dn
    state.last_token_switch     = time.time()
    state.pm_up                 = None
    state.pm_dn                 = None
    state.last_pm_update        = time.time()
    state.pm_price_frozen_count = 0
    state.pm_needs_reconnect    = True
    state.next_token_up         = None
    state.next_token_dn         = None
    state.next_token_prefetched = False
    # reconnection_in_progress stays True until pm_feed establishes the new WS connection


async def pm_feed(state: State, live_trader=None):
    if not state.pm_up_id:
        print("  [PM] no tokens for this coin/timeframe – skipped")
        return

    while True:
        # Always read from state so the watchdog can swap token IDs mid-run
        assets = [state.pm_up_id, state.pm_dn_id]

        # CLOB cooldown: Polymarket rate-limits WS handshakes and CLOB HTTP
        # requests from the same IP bucket. Wait if a trade just happened.
        if live_trader is not None:
            clob_age = time.time() - live_trader.last_clob_call
            if clob_age < 4.0:
                wait = 4.0 - clob_age
                print(f"  [PM] CLOB cooldown — waiting {wait:.1f}s before WS connect…")
                await asyncio.sleep(wait)

        try:
            async with websockets.connect(
                config.PM_WS,
                ping_interval=20,
                ping_timeout=60,
                close_timeout=10
            ) as ws:
                await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                print("  [PM] connected")
                state.last_pm_update           = time.time()
                state.pm_reconnecting          = False  # connection established
                state.reconnection_in_progress = False  # guard released once WS is up

                while True:
                    try:
                        # 5-second timeout so we can check pm_needs_reconnect
                        # even when the market is quiet
                        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        raw = json.loads(msg)

                        if isinstance(raw, list):
                            for entry in raw:
                                _pm_apply(entry.get("asset_id"), entry.get("asks", []), state)

                        elif isinstance(raw, dict) and raw.get("event_type") == "price_change":
                            for ch in raw.get("price_changes", []):
                                if ch.get("best_ask"):
                                    _pm_set(ch["asset_id"], float(ch["best_ask"]), state)

                    except asyncio.TimeoutError:
                        pass  # expected on quiet markets; fall through to flag check

                    except websockets.exceptions.ConnectionClosed:
                        print("  [PM] connection closed, reconnecting...")
                        break

                    # Watchdog requested a contract switch — break to outer loop
                    # which will re-read state.pm_up_id / state.pm_dn_id
                    if state.pm_needs_reconnect:
                        state.pm_needs_reconnect = False
                        print("  [PM] switching to new contract...")
                        break

        except Exception as e:
            err_str = str(e).lower()
            if "timed out" in err_str or "handshake" in err_str:
                backoff = random.uniform(15, 25)
                print(f"  [PM] connection timeout — backing off {backoff:.1f}s...")
                await asyncio.sleep(backoff)
            else:
                print(f"  [PM] connection error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)


def _pm_apply(asset, asks, state):
    if asks:
        _pm_set(asset, min(float(a["price"]) for a in asks), state)


def _pm_set(asset, price, state):
    if asset == state.pm_up_id:
        state.pm_up = price
        state.last_pm_update = time.time()
    elif asset == state.pm_dn_id:
        state.pm_dn = price
        state.last_pm_update = time.time()


def is_market_stale(state: State) -> tuple[bool, str]:
    """Return (is_stale, trigger) where trigger is 'timeout' or 'price_frozen'.

    Only meaningful once at least one PM message has been received
    (last_pm_update > 0).  Mutates pm_price_frozen_count as a side-effect.
    """
    # Never report stale while a reconnection is already in progress
    if state.reconnection_in_progress:
        state.pm_price_frozen_count = 0
        return False, ""

    if state.last_pm_update == 0.0:
        return False, ""

    # Condition A — no WebSocket messages for 30 s
    if time.time() - state.last_pm_update > 30:
        return True, "timeout"

    # Condition B — price pinned at resolution level for 10+ watchdog ticks
    if state.pm_up is not None:
        if state.pm_up >= 0.98 or state.pm_up <= 0.02:
            state.pm_price_frozen_count += 1
        else:
            state.pm_price_frozen_count = 0
        if state.pm_price_frozen_count >= 10:
            # Only report stale if the contract is confirmed expired,
            # or if we have no end-time info. While still within the
            # active window, a price near 0/1 is normal resolution
            # behavior — the proactive scheduler handles the switch.
            if state.market_end_time is None:
                return True, "price_frozen"
            if datetime.now(timezone.utc) >= state.market_end_time:
                return True, "price_frozen"
            # Contract still active — reset counter, let scheduler handle it
            state.pm_price_frozen_count = 0

    return False, ""

import sys
import os
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console
from rich.live    import Live

import config
import feeds
import dashboard
import indicators as ind
import scoring
import paper_trading

console = Console(force_terminal=True)

SIGNALS_LOG       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals_log.json")
PAPER_TRADES_LOG  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades.json")
RECONNECTION_LOG  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reconnection_log.json")

TREND_THRESH  = 3
DATA_INTERVAL = 0.2   # seconds between indicator computations (~5 Hz)
RENDER_FPS    = 30    # target render frames per second
RENDER_SLEEP  = 1 / RENDER_FPS


def pick(title: str, options: list[str]) -> str:
    console.print(f"\n[bold]{title}[/bold]")
    for i, o in enumerate(options, 1):
        console.print(f"  [{i}] {o}")
    while True:
        raw = input("  → ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        console.print("  [red]invalid – try again[/red]")


def _append_signal_log(event: dict):
    with open(SIGNALS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _append_reconnect_log(event: dict):
    with open(RECONNECTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _make_ds(coin: str, tf: str) -> dict:
    """Create the initial empty dashboard state dict."""
    return {
        # Identity
        "coin": coin, "tf": tf,
        # Raw market data
        "mid": 0.0, "bids": [], "asks": [],
        "pm_up": None, "pm_dn": None, "pm_up_id": None,
        "last_pm_update": 0.0,
        "pm_reconnecting": False, "reconnection_in_progress": False,
        "market_end_time": None,
        # Pre-computed indicators
        "obi": 0.0,
        "cvd_windows": {},
        "delta_1m": 0.0,
        "rsi": None,
        "macd_v": None, "macd_sig": None, "macd_hist": None,
        "vwap": None,
        "ema_s": None, "ema_l": None,
        "ha": [],
        "bias": 0.0,
        "walls_buy": [], "walls_sell": [],
        "depth": {},
        "poc": 0.0, "vol_profile": [],
        # Scoring
        "entry": {}, "divergence": {},
        "trend_score": 0, "trend_label": "NEUTRAL", "trend_col": "yellow",
        # Alert queue — drained by render_loop before each frame
        "pending_alerts": [],
        # Render control
        "render_hash": "",
        "ready": False,
    }


def _ds_hash(ds: dict) -> str:
    """Hash the fields that affect visual output to detect changes."""
    key = {
        "mid":         round(ds["mid"], 4),
        "pm_up":       ds["pm_up"],
        "pm_dn":       ds["pm_dn"],
        "trend":       ds["trend_label"],
        "trend_score": ds["trend_score"],
        "entry_score": ds.get("entry", {}).get("score", 0),
        "conviction":  ds.get("divergence", {}).get("conviction_level", ""),
        "pm_reconn":   ds["pm_reconnecting"],
        "obi":         round(ds["obi"], 3),
        "bias":        round(ds["bias"], 1),
    }
    return hashlib.md5(json.dumps(key, sort_keys=True).encode()).hexdigest()


async def pm_watchdog(state: feeds.State, coin: str, tf: str):
    """Reactive fallback: detects stale Polymarket contracts and triggers reconnection."""
    await asyncio.sleep(15)

    while True:
        await asyncio.sleep(5)

        if not state.pm_up_id:
            continue
        if state.pm_reconnecting or state.pm_needs_reconnect or state.reconnection_in_progress:
            continue

        stale, trigger = feeds.is_market_stale(state)
        if not stale:
            continue

        old_up_id = state.pm_up_id
        state.pm_reconnecting          = True
        state.reconnection_in_progress = True
        state.pm_price_frozen_count    = 0
        print(f"  [PM watchdog] market stale ({trigger}) — searching new contract for {coin} {tf}…")

        new_up, new_dn, new_end = None, None, None
        for attempt in range(7):
            if attempt > 0:
                await asyncio.sleep(10)
            try:
                up, dn, end_time = await feeds.fetch_pm_tokens_full_async(coin, tf)
                if up and up != old_up_id:
                    new_up, new_dn, new_end = up, dn, end_time
                    break
                if up == old_up_id:
                    print(f"  [PM watchdog] same contract still active, waiting… ({attempt + 1}/7)")
            except Exception as exc:
                print(f"  [PM watchdog] fetch error: {exc}")

        if new_up and new_dn:
            _append_reconnect_log({
                "timestamp":    datetime.now(timezone.utc).isoformat(),
                "coin":         coin,
                "timeframe":    tf,
                "old_token_id": old_up_id or "",
                "new_token_id": new_up,
                "trigger":      trigger,
            })
            feeds.apply_new_pm_tokens(state, new_up, new_dn)
            state.market_end_time = new_end
            state.pm_reconnecting = False
            print(f"  [PM watchdog] new contract acquired — {new_up[:24]}…")
        else:
            state.pm_reconnecting          = False
            state.reconnection_in_progress = False
            print(f"  [PM watchdog] no new contract found after retries — will retry next cycle")


async def pm_scheduler(state: feeds.State, coin: str, tf: str):
    """Proactive reconnection: switches to the next contract before the current one closes."""
    await asyncio.sleep(20)

    while True:
        await asyncio.sleep(10)

        if not state.pm_up_id or not state.market_end_time:
            continue
        if state.reconnection_in_progress:
            continue

        now       = datetime.now(timezone.utc)
        secs_left = (state.market_end_time - now).total_seconds()

        if 0 < secs_left <= 30 and not state.next_token_prefetched:
            state.next_token_prefetched = True
            up, dn = await feeds.prefetch_next_pm_tokens_async(coin, tf)
            if up:
                state.next_token_up = up
                state.next_token_dn = dn

        if secs_left <= 0:
            state.reconnection_in_progress = True
            new_up = state.next_token_up
            new_dn = state.next_token_dn

            if not new_up:
                print(f"  [PM scheduler] endDate reached — fetching next contract…")
                for attempt in range(6):
                    if attempt > 0:
                        await asyncio.sleep(10)
                    up, dn, end_time = await feeds.fetch_pm_tokens_full_async(coin, tf)
                    if up and up != state.pm_up_id:
                        new_up, new_dn = up, dn
                        state.market_end_time = end_time
                        break
                    print(f"  [PM scheduler] same/no contract, waiting… ({attempt + 1}/6)")
            else:
                _, _, end_time = await feeds.fetch_pm_tokens_full_async(coin, tf)
                if end_time:
                    state.market_end_time = end_time

            if new_up and new_dn and new_up != state.pm_up_id:
                old_up = state.pm_up_id
                _append_reconnect_log({
                    "timestamp":    datetime.now(timezone.utc).isoformat(),
                    "coin":         coin,
                    "timeframe":    tf,
                    "old_token_id": old_up or "",
                    "new_token_id": new_up,
                    "trigger":      "scheduled",
                })
                feeds.apply_new_pm_tokens(state, new_up, new_dn)
                print(f"  [PM scheduler] proactive switch done — {new_up[:24]}…")
            else:
                state.reconnection_in_progress = False
                print(f"  [PM scheduler] no new contract found — watchdog will retry")


async def data_loop(state: feeds.State, ds: dict, coin: str, tf: str,
                    trader: paper_trading.PaperTrader):
    """Compute all indicators at ~5 Hz and handle paper trading business logic.

    Writes pre-computed values to `ds` so render_loop can read them without
    performing any computation. Also queues alert messages in ds["pending_alerts"].
    """
    prev_conviction = "NEUTRAL"

    while True:
        await asyncio.sleep(DATA_INTERVAL)

        if not state.mid or not state.klines:
            continue

        bids, asks, mid = state.bids, state.asks, state.mid
        trades, klines  = state.trades, state.klines

        # ── Copy raw state fields ────────────────────────────────────────
        ds["mid"]                      = mid
        ds["bids"]                     = bids
        ds["asks"]                     = asks
        ds["pm_up"]                    = state.pm_up
        ds["pm_dn"]                    = state.pm_dn
        ds["pm_up_id"]                 = state.pm_up_id
        ds["last_pm_update"]           = state.last_pm_update
        ds["pm_reconnecting"]          = state.pm_reconnecting
        ds["reconnection_in_progress"] = state.reconnection_in_progress
        ds["market_end_time"]          = state.market_end_time

        # ── Compute every indicator exactly once ─────────────────────────
        ds["obi"]       = ind.obi(bids, asks, mid)
        ds["cvd_windows"] = {s: ind.cvd(trades, s) for s in config.CVD_WINDOWS}
        ds["delta_1m"]  = ind.cvd(trades, config.DELTA_WINDOW)
        ds["rsi"]       = ind.rsi(klines)
        macd_v, sig_v, hv = ind.macd(klines)
        ds["macd_v"], ds["macd_sig"], ds["macd_hist"] = macd_v, sig_v, hv
        ds["vwap"]      = ind.vwap(klines)
        ds["ema_s"], ds["ema_l"] = ind.emas(klines)
        ds["ha"]        = ind.heikin_ashi(klines)
        ds["bias"]      = ind.bias_score(bids, asks, mid, trades, klines)
        ds["walls_buy"], ds["walls_sell"] = ind.walls(bids, asks)
        ds["depth"]     = ind.depth_usd(bids, asks, mid) if mid else {}
        ds["poc"], ds["vol_profile"] = ind.vol_profile(klines)

        # ── Trend score (same logic as old _score_trend) ─────────────────
        t_score = 0
        obi_v   = ds["obi"]
        if obi_v > config.OBI_THRESH:    t_score += 1
        elif obi_v < -config.OBI_THRESH: t_score -= 1

        cvd5 = ds["cvd_windows"].get(300, 0)
        t_score += 1 if cvd5 > 0 else -1 if cvd5 < 0 else 0

        rsi_v = ds["rsi"]
        if rsi_v is not None:
            if rsi_v > config.RSI_OB:    t_score -= 1
            elif rsi_v < config.RSI_OS:  t_score += 1

        if hv is not None:
            t_score += 1 if hv > 0 else -1

        vwap_v = ds["vwap"]
        if vwap_v and mid:
            t_score += 1 if mid > vwap_v else -1

        ema_s, ema_l = ds["ema_s"], ds["ema_l"]
        if ema_s is not None and ema_l is not None:
            t_score += 1 if ema_s > ema_l else -1

        bw, aw = ds["walls_buy"], ds["walls_sell"]
        t_score += min(len(bw), 2)
        t_score -= min(len(aw), 2)

        ha = ds["ha"]
        if len(ha) >= 3:
            last3 = ha[-3:]
            if all(c["green"] for c in last3):   t_score += 1
            elif all(not c["green"] for c in last3): t_score -= 1

        if t_score >= TREND_THRESH:
            ds["trend_score"], ds["trend_label"], ds["trend_col"] = t_score, "BULLISH", "green"
        elif t_score <= -TREND_THRESH:
            ds["trend_score"], ds["trend_label"], ds["trend_col"] = t_score, "BEARISH", "red"
        else:
            ds["trend_score"], ds["trend_label"], ds["trend_col"] = t_score, "NEUTRAL", "yellow"

        # ── Entry score & divergence ─────────────────────────────────────
        entry      = scoring.calculate_entry_score(state)
        divergence = scoring.detect_divergence(state.pm_up, entry["score"])
        divergence["pm_up_price"] = state.pm_up
        ds["entry"]     = entry
        ds["divergence"] = divergence

        cl = divergence["conviction_level"]

        # ── Alert on new MAX_CONVICTION ──────────────────────────────────
        if cl in ("MAX_BULLISH", "MAX_BEARISH") and cl != prev_conviction:
            ts      = datetime.now(timezone.utc).isoformat()
            e_score = entry["score"]
            if cl == "MAX_BULLISH":
                ds["pending_alerts"].append(
                    f"\n[bold bright_green on dark_green] ⚡ MAX CONVICTION: LONG  — "
                    f"{coin} {tf}  Score {e_score:+d}  @ {mid:.4f}  {ts} "
                    f"[/bold bright_green on dark_green]\n"
                )
            else:
                ds["pending_alerts"].append(
                    f"\n[bold bright_red on dark_red] ⚡ MAX CONVICTION: SHORT — "
                    f"{coin} {tf}  Score {e_score:+d}  @ {mid:.4f}  {ts} "
                    f"[/bold bright_red on dark_red]\n"
                )
            print("\a", end="", flush=True)
            _append_signal_log({
                "timestamp":        ts,
                "coin":             coin,
                "timeframe":        tf,
                "score":            e_score,
                "direction":        entry["direction"],
                "price":            mid,
                "conditions":       [{"label": lbl, "pts": pts}
                                     for lbl, pts in entry["triggered_conditions"]],
                "pm_up_price":      divergence.get("pm_up_price"),
                "conviction_level": cl,
                "has_divergence":   divergence["has_divergence"],
            })

        # ── Open paper position on MAX_CONVICTION ────────────────────────
        if (cl in ("MAX_BULLISH", "MAX_BEARISH")
                and trader.current_open_position is None
                and state.pm_up is not None):
            signal = {
                "conviction_level":     cl,
                "score":                entry["score"],
                "triggered_conditions": entry["triggered_conditions"],
            }
            pos = trader.open_position(signal, state.pm_up, mid, coin, tf)
            if pos is None:
                ds["pending_alerts"].append(
                    f"[yellow]⏭️  Señal ignorada — precio PM fuera de rango válido: "
                    f"{state.pm_up:.3f}[/yellow]"
                )
            else:
                ds["pending_alerts"].append(
                    f"[cyan]📋 PAPER TRADE ABIERTO #{pos['id']}: "
                    f"{pos['direction']}  entry PM {pos['entry_pm_price']:.3f}  "
                    f"contracts {pos['contracts']:.2f}[/cyan]"
                )

        # ── Check resolution of open position ────────────────────────────
        open_pos = trader.current_open_position
        if open_pos is not None and state.pm_up is not None:
            updated = trader.check_resolution(open_pos, state.pm_up, mid)
            status  = updated["status"]
            if status in paper_trading.CLOSED_STATUSES:
                pnl      = updated["pnl"]
                wr       = trader.summary["win_rate"]
                exit_str = f"Salida: {updated['exit_pm_price']:.3f}"
                pnl_str  = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                _TRADE_MSGS = {
                    "WIN_TP":    f"[bold green]✅ WIN TP    {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold green]",
                    "WIN_TRAIL": f"[bold green]✅ WIN TRAIL {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold green]",
                    "WIN_FULL":  f"[bold green]✅ WIN FULL  {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold green]",
                    "LOSS_SL":   f"[bold red]🛑 LOSS SL   {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold red]",
                    "LOSS_FULL": f"[bold red]❌ LOSS FULL {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold red]",
                }
                if status in _TRADE_MSGS:
                    ds["pending_alerts"].append(_TRADE_MSGS[status])

        prev_conviction = cl
        ds["ready"] = True


async def render_loop(ds: dict, trader: paper_trading.PaperTrader):
    """Render the dashboard at 30fps using a persistent Live context.

    Reads only from the pre-computed `ds` dict — no indicator calculations here.
    Drains ds["pending_alerts"] through live.console before each frame so alerts
    appear above the live display.
    """
    await asyncio.sleep(2)

    last_hash = ""

    with Live(console=console, refresh_per_second=RENDER_FPS, transient=False) as live:
        while True:
            # Print any queued alerts above the live panel
            while ds["pending_alerts"]:
                live.console.print(ds["pending_alerts"].pop(0))

            if ds["ready"]:
                new_hash = _ds_hash(ds)
                if new_hash != last_hash:
                    live.update(dashboard.render(ds, trader))
                    last_hash = new_hash

            await asyncio.sleep(RENDER_SLEEP)


async def main():
    console.print("\n[bold magenta]═══ CRYPTO PREDICTION DASHBOARD ═══[/bold magenta]\n")

    coin = pick("Select coin:", config.COINS)
    tf   = pick("Select timeframe:", config.COIN_TIMEFRAMES[coin])

    console.print(f"\n[bold green]Starting {coin} {tf} …[/bold green]\n")

    trader = paper_trading.PaperTrader(PAPER_TRADES_LOG)
    s = trader.summary
    console.print(
        f"  [Paper Trading] trades: {s['total_trades']}  "
        f"W: {s['wins']}  L: {s['losses']}  "
        f"P&L: {'+'if s['total_pnl']>=0 else ''}${s['total_pnl']:.2f}\n"
    )

    state = feeds.State()
    state.pm_up_id, state.pm_dn_id, state.market_end_time = feeds.fetch_pm_tokens_full(coin, tf)
    if state.pm_up_id:
        console.print(f"  [PM] Up   → {state.pm_up_id[:24]}…")
        console.print(f"  [PM] Down → {state.pm_dn_id[:24]}…")
        if state.market_end_time:
            console.print(f"  [PM] Closes → {state.market_end_time.strftime('%H:%M:%S UTC')}")
    else:
        console.print("  [yellow][PM] no market for this coin/timeframe – prices will not show[/yellow]")

    binance_sym = config.COIN_BINANCE[coin]
    kline_iv    = config.TF_KLINE[tf]
    console.print("  [Binance] bootstrapping candles …")
    await feeds.bootstrap(binance_sym, kline_iv, state)

    ds = _make_ds(coin, tf)

    await asyncio.gather(
        feeds.ob_poller(binance_sym, state),
        feeds.binance_feed(binance_sym, kline_iv, state),
        feeds.pm_feed(state),
        pm_watchdog(state, coin, tf),
        pm_scheduler(state, coin, tf),
        data_loop(state, ds, coin, tf, trader),
        render_loop(ds, trader),
    )


if __name__ == "__main__":
    asyncio.run(main())

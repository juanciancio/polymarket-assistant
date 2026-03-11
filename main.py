import sys
import os
import asyncio
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console
from rich.live   import Live

import config
import feeds
import dashboard
import scoring
import paper_trading

console = Console(force_terminal=True)

SIGNALS_LOG      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals_log.json")
PAPER_TRADES_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades.json")


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
    """Append a single signal event as a JSON line to signals_log.json."""
    with open(SIGNALS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _emit_alert(live: Live, entry: dict, divergence: dict, coin: str, tf: str, mid: float):
    """Print a highlighted alert inside the Live context and write to log.

    Only called for MAX_BULLISH / MAX_BEARISH conviction levels.
    """
    cl    = divergence["conviction_level"]
    score = entry["score"]
    ts    = datetime.now(timezone.utc).isoformat()

    if cl == "MAX_BULLISH":
        live.console.print(
            f"\n[bold bright_green on dark_green] ⚡ MAX CONVICTION: LONG  — {coin} {tf}  Score {score:+d}  @ {mid:.4f}  {ts} [/bold bright_green on dark_green]\n"
        )
    else:
        live.console.print(
            f"\n[bold bright_red on dark_red] ⚡ MAX CONVICTION: SHORT — {coin} {tf}  Score {score:+d}  @ {mid:.4f}  {ts} [/bold bright_red on dark_red]\n"
        )

    # System beep
    print("\a", end="", flush=True)

    event = {
        "timestamp":       ts,
        "coin":            coin,
        "timeframe":       tf,
        "score":           score,
        "direction":       entry["direction"],
        "price":           mid,
        "conditions":      [{"label": lbl, "pts": pts} for lbl, pts in entry["triggered_conditions"]],
        "pm_up_price":     divergence.get("pm_up_price"),
        "conviction_level": cl,
        "has_divergence":  divergence["has_divergence"],
    }
    _append_signal_log(event)


async def display_loop(state: feeds.State, coin: str, tf: str,
                       trader: paper_trading.PaperTrader):
    await asyncio.sleep(2)
    refresh_interval = config.REFRESH_5M if tf == "5m" else config.REFRESH
    prev_conviction  = "NEUTRAL"

    with Live(console=console, refresh_per_second=1, transient=False) as live:
        while True:
            if state.mid > 0 and state.klines:
                entry      = scoring.calculate_entry_score(state)
                divergence = scoring.detect_divergence(state.pm_up, entry["score"])
                divergence["pm_up_price"] = state.pm_up

                cl = divergence["conviction_level"]

                # ── 1. Alert on new MAX_CONVICTION ───────────────────
                if cl in ("MAX_BULLISH", "MAX_BEARISH") and cl != prev_conviction:
                    _emit_alert(live, entry, divergence, coin, tf, state.mid)

                # ── 2. Open position on MAX_CONVICTION ───────────────
                if (cl in ("MAX_BULLISH", "MAX_BEARISH")
                        and trader.current_open_position is None
                        and state.pm_up is not None):
                    signal = {
                        "conviction_level":     cl,
                        "score":                entry["score"],
                        "triggered_conditions": entry["triggered_conditions"],
                    }
                    pos = trader.open_position(signal, state.pm_up, state.mid, coin, tf)
                    if pos is None:
                        live.console.print(
                            f"[yellow]⏭️  Señal ignorada — precio PM fuera de rango válido: "
                            f"{state.pm_up:.3f}[/yellow]"
                        )
                    else:
                        live.console.print(
                            f"[cyan]📋 PAPER TRADE ABIERTO #{pos['id']}: "
                            f"{pos['direction']}  entry PM {pos['entry_pm_price']:.3f}  "
                            f"contracts {pos['contracts']:.2f}[/cyan]"
                        )

                # ── 3. Check resolution of open position ─────────────
                open_pos = trader.current_open_position
                if open_pos is not None and state.pm_up is not None:
                    updated = trader.check_resolution(open_pos, state.pm_up, state.mid)
                    status  = updated["status"]
                    if status in paper_trading.CLOSED_STATUSES:
                        pnl  = updated["pnl"]
                        wr   = trader.summary["win_rate"]
                        exit_str = f"Salida: {updated['exit_pm_price']:.3f}"
                        pnl_str  = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                        if status == "WIN_TP":
                            live.console.print(
                                f"[bold green]✅ WIN TP    {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold green]"
                            )
                        elif status == "WIN_TRAIL":
                            live.console.print(
                                f"[bold green]✅ WIN TRAIL {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold green]"
                            )
                        elif status == "WIN_FULL":
                            live.console.print(
                                f"[bold green]✅ WIN FULL  {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold green]"
                            )
                        elif status == "LOSS_SL":
                            live.console.print(
                                f"[bold red]🛑 LOSS SL   {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold red]"
                            )
                        elif status == "LOSS_FULL":
                            live.console.print(
                                f"[bold red]❌ LOSS FULL {pnl_str} │ {exit_str} │ Win Rate: {wr:.1f}%[/bold red]"
                            )

                prev_conviction = cl
                live.update(dashboard.render(state, coin, tf, trader))

            await asyncio.sleep(refresh_interval)


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

    state.pm_up_id, state.pm_dn_id = feeds.fetch_pm_tokens(coin, tf)
    if state.pm_up_id:
        console.print(f"  [PM] Up   → {state.pm_up_id[:24]}…")
        console.print(f"  [PM] Down → {state.pm_dn_id[:24]}…")
    else:
        console.print("  [yellow][PM] no market for this coin/timeframe – prices will not show[/yellow]")

    binance_sym = config.COIN_BINANCE[coin]
    kline_iv    = config.TF_KLINE[tf]
    console.print("  [Binance] bootstrapping candles …")
    await feeds.bootstrap(binance_sym, kline_iv, state)

    await asyncio.gather(
        feeds.ob_poller(binance_sym, state),
        feeds.binance_feed(binance_sym, kline_iv, state),
        feeds.pm_feed(state),
        display_loop(state, coin, tf, trader),
    )


if __name__ == "__main__":
    asyncio.run(main())

from rich.table   import Table
from rich.panel   import Panel
from rich.console import Group
from rich.text    import Text
from rich         import box as bx

import config
import indicators as ind
import scoring
import paper_trading as pt


class _Group:
    def __init__(self, *renderables):
        self.renderables = renderables

    def __rich_console__(self, console, options):
        for r in self.renderables:
            yield r


def _p(val, d=2):
    if val is None:
        return "—"
    if val >= 1e6:
        return f"${val / 1e6:,.2f}M"
    if val >= 1e3:
        return f"${val:,.{d}f}"
    return f"${val:,.{d}f}"


def _col(val):
    if val is None:
        return "dim"
    return "green" if val > 0 else "red"


TREND_THRESH = 3


def _score_trend(st):
    score = 0

    obi_v = ind.obi(st.bids, st.asks, st.mid) if st.mid else 0.0
    if obi_v > config.OBI_THRESH:
        score += 1
    elif obi_v < -config.OBI_THRESH:
        score -= 1

    cvd5 = ind.cvd(st.trades, 300)
    score += 1 if cvd5 > 0 else -1 if cvd5 < 0 else 0

    rsi_v = ind.rsi(st.klines)
    if rsi_v is not None:
        if rsi_v > config.RSI_OB:
            score -= 1
        elif rsi_v < config.RSI_OS:
            score += 1

    _, _, hv = ind.macd(st.klines)
    if hv is not None:
        score += 1 if hv > 0 else -1

    vwap_v = ind.vwap(st.klines)
    if vwap_v and st.mid:
        score += 1 if st.mid > vwap_v else -1

    es, el = ind.emas(st.klines)
    if es is not None and el is not None:
        score += 1 if es > el else -1

    bw, aw = ind.walls(st.bids, st.asks)
    score += min(len(bw), 2)
    score -= min(len(aw), 2)

    ha = ind.heikin_ashi(st.klines)
    if len(ha) >= 3:
        last3 = ha[-3:]
        if all(c["green"] for c in last3):
            score += 1
        elif all(not c["green"] for c in last3):
            score -= 1

    if score >= TREND_THRESH:
        return score, "BULLISH",  "green"
    elif score <= -TREND_THRESH:
        return score, "BEARISH",  "red"
    else:
        return score, "NEUTRAL",  "yellow"


def _bias_display(bias: float) -> tuple[str, str, str]:
    """Return (label, pct_str, color) for a bias score in [-100, +100]."""
    pct = abs(bias)
    if bias > 10:
        label, col = "BULLISH", "green"
    elif bias < -10:
        label, col = "BEARISH", "red"
    else:
        label, col = "NEUTRAL", "yellow"
    return label, f"{pct:.0f}%", col


def _header(st, coin, tf):
    score, label, col = _score_trend(st)
    bias = ind.bias_score(st.bids, st.asks, st.mid, st.trades, st.klines)
    b_label, b_pct, b_col = _bias_display(bias)

    parts = [
        (f"  {coin} ", "bold white on dark_blue"),
        (f" {tf} ", "bold white on dark_green"),
        (f"  Price: {_p(st.mid)}  ", "bold white"),
    ]

    if st.pm_up is not None and st.pm_dn is not None:
        parts.append((f"  PM ↑ {st.pm_up:.3f}  ↓ {st.pm_dn:.3f}  ", "cyan"))

    parts.append((f" {label} ", f"bold white on {col}"))
    parts.append((f"  ({score:+d})", col))

    # ── Bias Score ────────────────────────────────────────────────
    parts.append(("   │  ", "dim"))
    parts.append(("Bias: ", "dim white"))
    parts.append((f"{b_label} {b_pct}", f"bold {b_col}"))

    parts.append(("\n", ""))
    parts.append(("  Polymarket Crypto Assistant", "dim white"))
    parts.append(("  |  @SolSt1ne", "dim cyan"))

    return Panel(
        Text.assemble(*parts),
        title="POLYMARKET CRYPTO ASSISTANT",
        box=bx.DOUBLE,
        expand=True,
    )


def _ob_panel(st):
    obi_v      = ind.obi(st.bids, st.asks, st.mid) if st.mid else 0.0
    bw, aw     = ind.walls(st.bids, st.asks)
    dep        = ind.depth_usd(st.bids, st.asks, st.mid) if st.mid else {}

    if obi_v > config.OBI_THRESH:
        oc, os = "green", "BULLISH"
    elif obi_v < -config.OBI_THRESH:
        oc, os = "red",   "BEARISH"
    else:
        oc, os = "yellow", "NEUTRAL"

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label", style="dim",    width=16)
    t.add_column("value",                 width=18)
    t.add_column("signal",                width=14)

    t.add_row("OBI",
              f"[{oc}]{obi_v * 100:+.1f} %[/{oc}]",
              f"[{oc}]{os}[/{oc}]")

    for pct in config.DEPTH_BANDS:
        t.add_row(f"Depth {pct}%", _p(dep.get(pct, 0)), "")

    if bw:
        t.add_row("BUY walls",
                  f"[green]{', '.join(_p(p, 2) for p, _ in bw[:3])}[/green]",
                  "[green]WALL[/green]")
    if aw:
        t.add_row("SELL walls",
                  f"[red]{', '.join(_p(p, 2) for p, _ in aw[:3])}[/red]",
                  "[red]WALL[/red]")
    if not bw and not aw:
        t.add_row("Walls", "[dim]none[/dim]", "")

    return Panel(t, title="ORDER BOOK", box=bx.ROUNDED, expand=True)


def _flow_panel(st):
    cvds = {s: ind.cvd(st.trades, s) for s in config.CVD_WINDOWS}
    poc, vp = ind.vol_profile(st.klines)

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label", style="dim", width=16)
    t.add_column("value",              width=22)
    t.add_column("dir",                width=4)

    for secs in config.CVD_WINDOWS:
        v  = cvds[secs]
        c  = _col(v)
        t.add_row(f"CVD {secs // 60}m",
                  f"[{c}]{_p(v)}[/{c}]",
                  f"[{c}]{'↑' if v > 0 else '↓'}[/{c}]")

    delta_v = ind.cvd(st.trades, config.DELTA_WINDOW)
    dc = _col(delta_v)
    t.add_row("Delta 1m",
              f"[{dc}]{_p(delta_v)}[/{dc}]",
              f"[{dc}]{'↑' if delta_v > 0 else '↓'}[/{dc}]")

    t.add_row("POC", f"[bold]{_p(poc)}[/bold]", "")

    if vp:
        max_v  = max(v for _, v in vp) or 1
        poc_i  = min(range(len(vp)), key=lambda i: abs(vp[i][0] - poc))
        half   = config.VP_SHOW // 2
        start  = max(0, poc_i - half)
        end    = min(len(vp), start + config.VP_SHOW)
        start  = max(0, end - config.VP_SHOW)

        for i in range(end - 1, start - 1, -1):
            p, v = vp[i]
            bar_len = int(v / max_v * 14)
            bar     = "█" * bar_len + "░" * (14 - bar_len)
            is_poc  = i == poc_i
            style   = "green bold" if is_poc else "dim"
            marker  = " ◄ POC" if is_poc else ""
            t.add_row(f"[{style}]{_p(p)}[/{style}]",
                      f"[{style}]{bar}{marker}[/{style}]", "")

    return Panel(t, title="FLOW & VOLUME", box=bx.ROUNDED, expand=True)


def _ta_panel(st):
    rsi_v              = ind.rsi(st.klines)
    macd_v, sig_v, hv  = ind.macd(st.klines)
    vwap_v             = ind.vwap(st.klines)
    ema_s, ema_l       = ind.emas(st.klines)
    ha                 = ind.heikin_ashi(st.klines)

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label",  style="dim", width=16)
    t.add_column("value",              width=18)
    t.add_column("signal",             width=18)

    if rsi_v is not None:
        if rsi_v > config.RSI_OB:
            rc, rs = "red",   "OVERBOUGHT"
        elif rsi_v < config.RSI_OS:
            rc, rs = "green", "OVERSOLD"
        else:
            rc, rs = "yellow", f"{rsi_v:.0f}"
        t.add_row("RSI(14)", f"[{rc}]{rsi_v:.1f}[/{rc}]", f"[{rc}]{rs}[/{rc}]")
    else:
        t.add_row("RSI(14)", "[dim]—[/dim]", "")

    if macd_v is not None:
        mc = _col(macd_v)
        t.add_row("MACD", f"[{mc}]{macd_v:+.6f}[/{mc}]", f"[{mc}]{'↑' if macd_v > 0 else '↓'}[/{mc}]")
        if sig_v is not None:
            cross = "[green]bullish[/green]" if hv is not None and hv > 0 else "[red]bearish[/red]"
            t.add_row("Signal", f"{sig_v:+.6f}", cross)
    else:
        t.add_row("MACD", "[dim]—[/dim]", "")

    if vwap_v and st.mid:
        vc  = "green" if st.mid > vwap_v else "red"
        vr  = "above" if st.mid > vwap_v else "below"
        t.add_row("VWAP", _p(vwap_v), f"[{vc}]price {vr}[/{vc}]")

    if ema_s is not None and ema_l is not None:
        ec  = "green" if ema_s > ema_l else "red"
        rel = ">" if ema_s > ema_l else "<"
        t.add_row("EMA 5",  _p(ema_s), f"[{ec}]{rel} EMA 20[/{ec}]")
        t.add_row("EMA 20", _p(ema_l), "")

    if ha:
        last = ha[-config.HA_COUNT:]
        dots = " ".join("[green]▲[/green]" if c["green"] else "[red]▼[/red]" for c in last)
        green_tail = sum(1 for c in last[-3:] if c["green"])
        hc   = "green" if green_tail >= 2 else "red"
        hs   = "trend ↑" if green_tail >= 2 else "trend ↓"
        t.add_row("Heikin Ashi", dots, f"[{hc}]{hs}[/{hc}]")

    return Panel(t, title="TECHNICAL", box=bx.ROUNDED, expand=True)


def _signals_panel(st):
    sigs = []

    obi_v = ind.obi(st.bids, st.asks, st.mid) if st.mid else 0.0
    if abs(obi_v) > config.OBI_THRESH:
        c = "green" if obi_v > 0 else "red"
        d = "BULLISH" if obi_v > 0 else "BEARISH"
        sigs.append(f"[{c}]OBI → {d} ({obi_v * 100:+.1f} %)[/{c}]")

    cvd5 = ind.cvd(st.trades, 300)
    if cvd5 != 0:
        c = "green" if cvd5 > 0 else "red"
        d = "buy pressure" if cvd5 > 0 else "sell pressure"
        sigs.append(f"[{c}]CVD 5m → {d} ({_p(cvd5)})[/{c}]")

    rsi_v = ind.rsi(st.klines)
    if rsi_v is not None:
        if rsi_v > config.RSI_OB:
            sigs.append(f"[red]RSI → overbought ({rsi_v:.0f})[/red]")
        elif rsi_v < config.RSI_OS:
            sigs.append(f"[green]RSI → oversold ({rsi_v:.0f})[/green]")

    _, _, hv = ind.macd(st.klines)
    if hv is not None:
        c = "green" if hv > 0 else "red"
        d = "bullish" if hv > 0 else "bearish"
        sigs.append(f"[{c}]MACD hist → {d}[/{c}]")

    vwap_v = ind.vwap(st.klines)
    if vwap_v and st.mid:
        c = "green" if st.mid > vwap_v else "red"
        d = "above" if st.mid > vwap_v else "below"
        sigs.append(f"[{c}]Price {d} VWAP[/{c}]")

    es, el = ind.emas(st.klines)
    if es is not None and el is not None:
        c = "green" if es > el else "red"
        d = "golden" if es > el else "death"
        sigs.append(f"[{c}]EMA → {d} cross[/{c}]")

    bw, aw = ind.walls(st.bids, st.asks)
    if bw:
        sigs.append(f"[green]BUY wall × {len(bw)} levels[/green]")
    if aw:
        sigs.append(f"[red]SELL wall × {len(aw)} levels[/red]")

    ha = ind.heikin_ashi(st.klines)
    if len(ha) >= 3:
        last3 = ha[-3:]
        if all(c["green"] for c in last3):
            sigs.append("[green]HA → 3+ green candles (up streak)[/green]")
        elif all(not c["green"] for c in last3):
            sigs.append("[red]HA → 3+ red candles (down streak)[/red]")

    if not sigs:
        sigs.append("[dim]No active signals[/dim]")

    score, label, col = _score_trend(st)
    bias = ind.bias_score(st.bids, st.asks, st.mid, st.trades, st.klines)
    b_label, b_pct, b_col = _bias_display(bias)

    # ── TREND bar (qualitative) ─────────────────────────────────
    max_score = 10
    filled = int(min(abs(score), max_score) / max_score * 14)
    bar    = "█" * filled + "░" * (14 - filled)
    sigs.append("[dim]─────────────────────────────[/dim]")
    sigs.append(f"[{col} bold]TREND: {label}[/{col} bold]  "
                f"[{col}]{bar}[/{col}]  [{col}]{score:+d}[/{col}]")

    # ── BIAS SCORE bar (quantitative) ───────────────────────────
    bias_filled = int(abs(bias) / 100 * 20)
    bias_bar    = "█" * bias_filled + "░" * (20 - bias_filled)
    sign_arrow  = "▲" if bias >= 0 else "▼"
    sigs.append(f"[{b_col} bold]BIAS:  {b_label} {b_pct:>5}[/{b_col} bold]  "
                f"[{b_col}]{sign_arrow} {bias_bar}[/{b_col}]  "
                f"[dim]{bias:+.1f}[/dim]")

    # ── ENTRY SCORE ─────────────────────────────────────────────
    entry = scoring.calculate_entry_score(st)
    e_score = entry["score"]
    e_dir   = entry["direction"]
    e_col   = "green" if e_dir == "BULLISH" else "red" if e_dir == "BEARISH" else "dim"
    e_filled = int(min(abs(e_score), 7) / 7 * 20)
    e_bar    = "█" * e_filled + "░" * (20 - e_filled)
    e_arrow  = "▲" if e_score > 0 else "▼" if e_score < 0 else "─"

    sigs.append("[dim]─────────────────────────────[/dim]")
    sigs.append(f"[{e_col} bold]ENTRY: {e_dir:<8}[/{e_col} bold]  "
                f"[{e_col}]{e_arrow} {e_bar}[/{e_col}]  "
                f"[dim]{e_score:+d}[/dim]")

    if entry["triggered_conditions"]:
        parts = "  ".join(
            f"[{'green' if pts > 0 else 'red'}]{label} ({pts:+d})[/{'green' if pts > 0 else 'red'}]"
            for label, pts in entry["triggered_conditions"]
        )
        sigs.append(f"  {parts}")

    # ── CONVICTION / DIVERGENCE banner ───────────────────────────
    div = scoring.detect_divergence(st.pm_up, e_score)
    cl  = div["conviction_level"]
    if cl == "MAX_BULLISH":
        sigs.append("[bold bright_green on dark_green] ⚡ MAX CONVICTION: LONG [/bold bright_green on dark_green]")
    elif cl == "MAX_BEARISH":
        sigs.append("[bold bright_red on dark_red] ⚡ MAX CONVICTION: SHORT [/bold bright_red on dark_red]")
    elif cl == "DIVERGENCE":
        sigs.append("[bold yellow] ⚠️  DIVERGENCE — Polymarket vs Técnicos, esperar [/bold yellow]")

    return Panel("\n".join(sigs), title="SIGNALS", box=bx.ROUNDED, expand=True)


def _paper_panel(trader: "pt.PaperTrader", st) -> Panel:
    s   = trader.summary
    wr  = s["win_rate"]
    wr_col  = "green" if wr >= 55 else "yellow" if wr >= 45 else "red"
    pnl_col = "green" if s["total_pnl"] >= 0 else "red"
    avg_col = "green" if s["avg_pnl_per_trade"] >= 0 else "red"

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label", style="dim", width=20)
    t.add_column("value",              width=36)

    t.add_row(
        "Trades / W / L",
        f"{s['total_trades']}  │  [green]W: {s['wins']}[/green]  │  [red]L: {s['losses']}[/red]",
    )
    t.add_row(
        "Win Rate",
        f"[{wr_col} bold]{wr:.1f}%[/{wr_col} bold]",
    )
    t.add_row(
        "P&L Total",
        f"[{pnl_col}]{'+' if s['total_pnl'] >= 0 else ''}${s['total_pnl']:.2f}[/{pnl_col}]",
    )
    t.add_row(
        "Avg / trade",
        f"[{avg_col}]{'+' if s['avg_pnl_per_trade'] >= 0 else ''}${s['avg_pnl_per_trade']:.2f}[/{avg_col}]",
    )

    open_pos = trader.current_open_position
    if open_pos:
        direction = open_pos["direction"]
        entry_pm  = open_pos["entry_pm_price"]
        tp        = open_pos.get("tp_target", entry_pm + 0.10)
        sl        = open_pos.get("sl_target", entry_pm - 0.15)
        highest   = open_pos.get("highest_price", entry_pm)
        d_col     = "green" if direction == "LONG" else "red"
        coin_tf   = f"{open_pos['coin']} {open_pos['timeframe']}"

        cur_contract = (
            (st.pm_up if direction == "LONG" else (1.0 - st.pm_up))
            if st.pm_up is not None else None
        )

        t.add_row("", "")
        t.add_row(
            "POSICIÓN ABIERTA",
            f"[{d_col} bold]{direction}[/{d_col} bold]  {coin_tf}",
        )
        t.add_row(
            "Entrada",
            f"${entry_pm:.3f}  │  "
            f"[green]TP: ${tp:.3f}[/green]  │  "
            f"[red]SL: ${sl:.3f}[/red]",
        )
        if cur_contract is not None:
            upnl  = trader.unrealized_pnl(st.pm_up)
            u_col = "green" if (upnl or 0) >= 0 else "red"
            arrow = "↑" if (upnl or 0) >= 0 else "↓"
            trail_stop = round(highest - 0.12, 3)
            high_str = (
                f"  │  [yellow]trail stop: ${trail_stop:.3f}[/yellow]"
                if highest >= 0.75 else ""
            )
            t.add_row(
                "Actual",
                f"${cur_contract:.3f}{high_str}",
            )
            if upnl is not None:
                t.add_row(
                    "P&L flotante",
                    f"[{u_col} bold]{'+' if upnl >= 0 else ''}${upnl:.2f} {arrow}[/{u_col} bold]",
                )
    else:
        t.add_row("", "")
        t.add_row("Estado", "[dim]Sin posición abierta — esperando señal[/dim]")

    return Panel(t, title="PAPER TRADING", box=bx.ROUNDED, expand=True)


def render(st, coin, tf, trader: "pt.PaperTrader | None" = None) -> "_Group":
    header = _header(st, coin, tf)

    grid = Table(box=None, pad_edge=False, show_header=False, expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(
        Group(_ob_panel(st), _ta_panel(st)),
        _flow_panel(st),
    )

    panels: list = [header, grid, _signals_panel(st)]
    if trader is not None:
        panels.append(_paper_panel(trader, st))

    return _Group(*panels)

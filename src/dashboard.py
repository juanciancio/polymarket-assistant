import time as _time
from datetime import datetime, timezone as _tz

from rich.table   import Table
from rich.panel   import Panel
from rich.console import Group
from rich.text    import Text
from rich         import box as bx

import config
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


def _bias_display(bias: float) -> tuple[str, str, str]:
    pct = abs(bias)
    if bias > 10:
        label, col = "BULLISH", "green"
    elif bias < -10:
        label, col = "BEARISH", "red"
    else:
        label, col = "NEUTRAL", "yellow"
    return label, f"{pct:.0f}%", col


# ── Panel builders — all accept pre-computed `ds` dict ────────────────────────

def _header(ds: dict) -> Panel:
    score = ds["trend_score"]
    label = ds["trend_label"]
    col   = ds["trend_col"]
    bias  = ds["bias"]
    b_label, b_pct, b_col = _bias_display(bias)
    coin  = ds["coin"]
    tf    = ds["tf"]
    mid   = ds["mid"]
    pm_up = ds["pm_up"]
    pm_dn = ds["pm_dn"]

    parts = [
        (f"  {coin} ", "bold white on dark_blue"),
        (f" {tf} ",    "bold white on dark_green"),
        (f"  Price: {_p(mid)}  ", "bold white"),
    ]

    if pm_up is not None and pm_dn is not None:
        parts.append((f"  PM ↑ {pm_up:.3f}  ↓ {pm_dn:.3f}  ", "cyan"))

    if ds["pm_up_id"] is not None:
        pm_age = _time.time() - ds["last_pm_update"] if ds["last_pm_update"] > 0 else float("inf")
        if ds["pm_reconnecting"] or ds["reconnection_in_progress"]:
            parts.append(("  ◌ RECONECTANDO...", "yellow"))
        elif pm_age > 60:
            parts.append(("  ✕ SIN DATOS", "red"))
        elif pm_up is not None:
            parts.append(("  ● LIVE", "green"))

    end_time = ds["market_end_time"]
    if end_time is not None:
        secs_left = (end_time - datetime.now(_tz.utc)).total_seconds()
        if 0 < secs_left <= 60:
            parts.append((f"  ⏱ {int(secs_left)}s", "bold yellow"))
        elif 0 < secs_left <= 300:
            m, s = divmod(int(secs_left), 60)
            parts.append((f"  ⏱ {m}m{s:02d}s", "dim cyan"))

    parts.append((f" {label} ", f"bold white on {col}"))
    parts.append((f"  ({score:+d})", col))
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


def _ob_panel(ds: dict) -> Panel:
    obi_v = ds["obi"]
    bw    = ds["walls_buy"]
    aw    = ds["walls_sell"]
    dep   = ds["depth"]

    if obi_v > config.OBI_THRESH:
        oc, os = "green", "BULLISH"
    elif obi_v < -config.OBI_THRESH:
        oc, os = "red",   "BEARISH"
    else:
        oc, os = "yellow", "NEUTRAL"

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label", style="dim", width=16)
    t.add_column("value",             width=18)
    t.add_column("signal",            width=14)

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


def _flow_panel(ds: dict) -> Panel:
    cvd_windows = ds["cvd_windows"]
    poc         = ds["poc"]
    vp          = ds["vol_profile"]

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label", style="dim", width=16)
    t.add_column("value",              width=22)
    t.add_column("dir",                width=4)

    for secs in config.CVD_WINDOWS:
        v = cvd_windows.get(secs, 0)
        c = _col(v)
        t.add_row(f"CVD {secs // 60}m",
                  f"[{c}]{_p(v)}[/{c}]",
                  f"[{c}]{'↑' if v > 0 else '↓'}[/{c}]")

    delta_v = ds["delta_1m"]
    dc = _col(delta_v)
    t.add_row("Delta 1m",
              f"[{dc}]{_p(delta_v)}[/{dc}]",
              f"[{dc}]{'↑' if delta_v > 0 else '↓'}[/{dc}]")

    t.add_row("POC", f"[bold]{_p(poc)}[/bold]", "")

    if vp:
        max_v = max(v for _, v in vp) or 1
        poc_i = min(range(len(vp)), key=lambda i: abs(vp[i][0] - poc))
        half  = config.VP_SHOW // 2
        start = max(0, poc_i - half)
        end   = min(len(vp), start + config.VP_SHOW)
        start = max(0, end - config.VP_SHOW)

        for i in range(end - 1, start - 1, -1):
            p, v    = vp[i]
            bar_len = int(v / max_v * 14)
            bar     = "█" * bar_len + "░" * (14 - bar_len)
            is_poc  = i == poc_i
            style   = "green bold" if is_poc else "dim"
            marker  = " ◄ POC" if is_poc else ""
            t.add_row(f"[{style}]{_p(p)}[/{style}]",
                      f"[{style}]{bar}{marker}[/{style}]", "")

    return Panel(t, title="FLOW & VOLUME", box=bx.ROUNDED, expand=True)


def _ta_panel(ds: dict) -> Panel:
    rsi_v  = ds["rsi"]
    macd_v = ds["macd_v"]
    sig_v  = ds["macd_sig"]
    hv     = ds["macd_hist"]
    vwap_v = ds["vwap"]
    ema_s  = ds["ema_s"]
    ema_l  = ds["ema_l"]
    ha     = ds["ha"]
    mid    = ds["mid"]

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label",  style="dim", width=16)
    t.add_column("value",              width=18)
    t.add_column("signal",             width=18)

    if rsi_v is not None:
        if rsi_v > config.RSI_OB:
            rc, rs = "red",    "OVERBOUGHT"
        elif rsi_v < config.RSI_OS:
            rc, rs = "green",  "OVERSOLD"
        else:
            rc, rs = "yellow", f"{rsi_v:.0f}"
        t.add_row("RSI(14)", f"[{rc}]{rsi_v:.1f}[/{rc}]", f"[{rc}]{rs}[/{rc}]")
    else:
        t.add_row("RSI(14)", "[dim]—[/dim]", "")

    if macd_v is not None:
        mc = _col(macd_v)
        t.add_row("MACD", f"[{mc}]{macd_v:+.6f}[/{mc}]",
                  f"[{mc}]{'↑' if macd_v > 0 else '↓'}[/{mc}]")
        if sig_v is not None:
            cross = "[green]bullish[/green]" if hv is not None and hv > 0 else "[red]bearish[/red]"
            t.add_row("Signal", f"{sig_v:+.6f}", cross)
    else:
        t.add_row("MACD", "[dim]—[/dim]", "")

    if vwap_v and mid:
        vc  = "green" if mid > vwap_v else "red"
        vr  = "above" if mid > vwap_v else "below"
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

    vol = ds.get("volatility", -1.0)
    if vol < 0:
        t.add_row("Volatility 60s", "[dim]—[/dim]", "[dim]sin datos[/dim]")
    elif vol < 0.10:
        t.add_row("Volatility 60s",
                  f"[bold magenta]{vol:.3f}%[/bold magenta]",
                  "[bold magenta]ULTRA ESTABLE[/bold magenta]")
    elif vol < 0.20:
        t.add_row("Volatility 60s",
                  f"[bold green]{vol:.3f}%[/bold green]",
                  "[bold green]MUY ESTABLE[/bold green]")
    elif vol < 0.30:
        t.add_row("Volatility 60s",
                  f"[green]{vol:.3f}%[/green]",
                  "[green]ESTABLE[/green]")
    elif vol < 0.50:
        t.add_row("Volatility 60s",
                  f"[yellow]{vol:.3f}%[/yellow]",
                  "[yellow]NORMAL[/yellow]")
    else:
        t.add_row("Volatility 60s",
                  f"[red]{vol:.3f}%[/red]",
                  "[red]VOLÁTIL[/red]")

    return Panel(t, title="TECHNICAL", box=bx.ROUNDED, expand=True)


def _signals_panel(ds: dict) -> Panel:
    sigs   = []
    obi_v  = ds["obi"]
    cvd5   = ds["cvd_windows"].get(300, 0)
    rsi_v  = ds["rsi"]
    hv     = ds["macd_hist"]
    vwap_v = ds["vwap"]
    mid    = ds["mid"]
    ema_s  = ds["ema_s"]
    ema_l  = ds["ema_l"]
    bw     = ds["walls_buy"]
    aw     = ds["walls_sell"]
    ha     = ds["ha"]
    bias   = ds["bias"]

    if abs(obi_v) > config.OBI_THRESH:
        c = "green" if obi_v > 0 else "red"
        d = "BULLISH" if obi_v > 0 else "BEARISH"
        sigs.append(f"[{c}]OBI → {d} ({obi_v * 100:+.1f} %)[/{c}]")

    if cvd5 != 0:
        c = "green" if cvd5 > 0 else "red"
        d = "buy pressure" if cvd5 > 0 else "sell pressure"
        sigs.append(f"[{c}]CVD 5m → {d} ({_p(cvd5)})[/{c}]")

    if rsi_v is not None:
        if rsi_v > config.RSI_OB:
            sigs.append(f"[red]RSI → overbought ({rsi_v:.0f})[/red]")
        elif rsi_v < config.RSI_OS:
            sigs.append(f"[green]RSI → oversold ({rsi_v:.0f})[/green]")

    if hv is not None:
        c = "green" if hv > 0 else "red"
        d = "bullish" if hv > 0 else "bearish"
        sigs.append(f"[{c}]MACD hist → {d}[/{c}]")

    if vwap_v and mid:
        c = "green" if mid > vwap_v else "red"
        d = "above" if mid > vwap_v else "below"
        sigs.append(f"[{c}]Price {d} VWAP[/{c}]")

    if ema_s is not None and ema_l is not None:
        c = "green" if ema_s > ema_l else "red"
        d = "golden" if ema_s > ema_l else "death"
        sigs.append(f"[{c}]EMA → {d} cross[/{c}]")

    if bw:
        sigs.append(f"[green]BUY wall × {len(bw)} levels[/green]")
    if aw:
        sigs.append(f"[red]SELL wall × {len(aw)} levels[/red]")

    if len(ha) >= 3:
        last3 = ha[-3:]
        if all(c["green"] for c in last3):
            sigs.append("[green]HA → 3+ green candles (up streak)[/green]")
        elif all(not c["green"] for c in last3):
            sigs.append("[red]HA → 3+ red candles (down streak)[/red]")

    if not sigs:
        sigs.append("[dim]No active signals[/dim]")

    score = ds["trend_score"]
    label = ds["trend_label"]
    col   = ds["trend_col"]
    b_label, b_pct, b_col = _bias_display(bias)

    max_score = 10
    filled = int(min(abs(score), max_score) / max_score * 14)
    bar    = "█" * filled + "░" * (14 - filled)
    sigs.append("[dim]─────────────────────────────[/dim]")
    sigs.append(f"[{col} bold]TREND: {label}[/{col} bold]  "
                f"[{col}]{bar}[/{col}]  [{col}]{score:+d}[/{col}]")

    bias_filled = int(abs(bias) / 100 * 20)
    bias_bar    = "█" * bias_filled + "░" * (20 - bias_filled)
    sign_arrow  = "▲" if bias >= 0 else "▼"
    sigs.append(f"[{b_col} bold]BIAS:  {b_label} {b_pct:>5}[/{b_col} bold]  "
                f"[{b_col}]{sign_arrow} {bias_bar}[/{b_col}]  "
                f"[dim]{bias:+.1f}[/dim]")

    entry   = ds.get("entry", {})
    e_score = entry.get("score", 0)
    e_dir   = entry.get("direction", "NEUTRAL")
    e_col   = "green" if e_dir == "BULLISH" else "red" if e_dir == "BEARISH" else "dim"
    e_filled = int(min(abs(e_score), 7) / 7 * 20)
    e_bar    = "█" * e_filled + "░" * (20 - e_filled)
    e_arrow  = "▲" if e_score > 0 else "▼" if e_score < 0 else "─"

    sigs.append("[dim]─────────────────────────────[/dim]")
    sigs.append(f"[{e_col} bold]ENTRY: {e_dir:<8}[/{e_col} bold]  "
                f"[{e_col}]{e_arrow} {e_bar}[/{e_col}]  "
                f"[dim]{e_score:+d}[/dim]")

    if entry.get("triggered_conditions"):
        parts = "  ".join(
            f"[{'green' if pts > 0 else 'red'}]{lbl} ({pts:+d})[/{'green' if pts > 0 else 'red'}]"
            for lbl, pts in entry["triggered_conditions"]
        )
        sigs.append(f"  {parts}")

    div = ds.get("divergence", {})
    cl  = div.get("conviction_level", "NEUTRAL")
    if cl == "MAX_BULLISH":
        sigs.append("[bold bright_green on dark_green] ⚡ MAX CONVICTION: LONG [/bold bright_green on dark_green]")
    elif cl == "MAX_BEARISH":
        sigs.append("[bold bright_red on dark_red] ⚡ MAX CONVICTION: SHORT [/bold bright_red on dark_red]")
    elif cl == "DIVERGENCE":
        sigs.append("[bold yellow] ⚠️  DIVERGENCE — Polymarket vs Técnicos, esperar [/bold yellow]")

    return Panel("\n".join(sigs), title="SIGNALS", box=bx.ROUNDED, expand=True)


def _paper_panel(trader: "pt.PaperTrader", ds: dict) -> Panel:
    s       = trader.summary
    wr      = s["win_rate"]
    wr_col  = "green" if wr >= 55 else "yellow" if wr >= 45 else "red"
    pnl_col = "green" if s["total_pnl"] >= 0 else "red"
    avg_col = "green" if s["avg_pnl_per_trade"] >= 0 else "red"
    pm_up   = ds["pm_up"]

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label", style="dim", width=20)
    t.add_column("value",              width=36)

    t.add_row(
        "Trades / W / L",
        f"{s['total_trades']}  │  [green]W: {s['wins']}[/green]  │  [red]L: {s['losses']}[/red]",
    )
    t.add_row("Win Rate",   f"[{wr_col} bold]{wr:.1f}%[/{wr_col} bold]")
    t.add_row("P&L Total",  f"[{pnl_col}]{'+' if s['total_pnl'] >= 0 else ''}${s['total_pnl']:.2f}[/{pnl_col}]")
    t.add_row("Avg / trade", f"[{avg_col}]{'+' if s['avg_pnl_per_trade'] >= 0 else ''}${s['avg_pnl_per_trade']:.2f}[/{avg_col}]")
    t.add_row("Umbral",     "[green]LONG ≥+5[/green]  │  [red]SHORT ≤-5[/red]")

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
            (pm_up if direction == "LONG" else (1.0 - pm_up))
            if pm_up is not None else None
        )

        t.add_row("", "")
        t.add_row("POSICIÓN ABIERTA",
                  f"[{d_col} bold]{direction}[/{d_col} bold]  {coin_tf}")
        t.add_row("Entrada",
                  f"${entry_pm:.3f}  │  "
                  f"[green]TP: ${tp:.3f}[/green]  │  "
                  f"[red]SL: ${sl:.3f}[/red]")
        if cur_contract is not None:
            upnl  = trader.unrealized_pnl(pm_up)
            u_col = "green" if (upnl or 0) >= 0 else "red"
            arrow = "↑" if (upnl or 0) >= 0 else "↓"
            trail_stop = round(highest - 0.12, 3)
            high_str = (
                f"  │  [yellow]trail stop: ${trail_stop:.3f}[/yellow]"
                if highest >= 0.75 else ""
            )
            t.add_row("Actual", f"${cur_contract:.3f}{high_str}")
            if upnl is not None:
                t.add_row("P&L flotante",
                          f"[{u_col} bold]{'+' if upnl >= 0 else ''}${upnl:.2f} {arrow}[/{u_col} bold]")
    else:
        t.add_row("", "")
        cooldown = trader.cooldown_remaining
        if cooldown > 0:
            m, s2 = divmod(cooldown, 60)
            t.add_row("Estado",
                      f"[yellow]⏳ Cooldown: {cooldown}s — próxima entrada en {m:02d}:{s2:02d}[/yellow]")
        elif trader.is_burst_limited:
            t.add_row("Estado",
                      f"[yellow]⚠️  Límite de trades por ventana ({pt.BURST_MAX}/{pt.BURST_WINDOW // 60}min)[/yellow]")
        else:
            t.add_row("Estado", "[dim]Sin posición abierta — esperando señal[/dim]")

    return Panel(t, title="PAPER TRADING", box=bx.ROUNDED, expand=True)


def _live_panel(live_trader, ds: dict) -> Panel:
    """Live trading status panel. live_trader is a LiveTrader instance."""
    s = live_trader.summary

    if not live_trader.live_trading:
        return Panel(
            "[dim]⚪ Live trading desactivado — activar LIVE_TRADING=true en .env[/dim]",
            title="LIVE TRADING",
            box=bx.ROUNDED,
            expand=True,
        )

    t = Table(box=None, show_header=False, pad_edge=False, expand=True)
    t.add_column("label", style="dim", width=22)
    t.add_column("value",              width=40)

    t.add_row("", "[bold red]🔴 LIVE MODE ACTIVO — Capital real[/bold red]")

    wr     = s["win_rate"]
    wr_col = "green" if wr >= 65 else "yellow" if wr >= 55 else "red"
    t.add_row(
        "Trades / W / L",
        f"{s['total_trades']}  │  [green]W: {s['wins']}[/green]  │  [red]L: {s['losses']}[/red]  │  "
        f"[{wr_col} bold]Win Rate: {wr:.1f}%[/{wr_col} bold]",
    )

    pnl     = s["total_pnl_usd"]
    pnl_col = "green" if pnl >= 0 else "red"
    dpnl    = live_trader.daily_pnl
    dp_col  = "green" if dpnl >= 0 else "red"
    avail   = live_trader.daily_loss_limit + dpnl
    t.add_row(
        "P&L Total / Diario",
        f"[{pnl_col}]{'+' if pnl >= 0 else ''}${pnl:.2f}[/{pnl_col}]"
        f"  │  [{dp_col}]{'+' if dpnl >= 0 else ''}${dpnl:.2f} hoy[/{dp_col}]",
    )
    t.add_row(
        "Daily Loss Limit",
        f"${live_trader.daily_loss_limit:.2f}  │  Disponible: ${avail:.2f}",
    )

    slip_pct = s["avg_slippage"] * 100
    t.add_row(
        "Avg Slippage / FOK cancel",
        f"{slip_pct:.2f}%  │  {s.get('fok_canceled_count', 0)} cancelados",
    )

    # ── Special banners ──────────────────────────────────────────────────
    if live_trader.daily_pnl <= -live_trader.daily_loss_limit:
        t.add_row("", "[bold red]🚨 DAILY LOSS LIMIT — Trading detenido[/bold red]")

    # CLOSE_FAILED alert — show token and capital at risk for recent failures
    _now = datetime.now(_tz.utc)
    _recent_cf = [
        p for p in live_trader._data.get("positions", [])
        if p.get("status") == "CLOSE_FAILED"
        and p.get("timestamp_close")
        and (_now - datetime.fromisoformat(p["timestamp_close"])).total_seconds() < 600
    ]
    if _recent_cf:
        for _cf in _recent_cf:
            _tid  = _cf.get("token_id", "")[:16]
            _cap  = _cf.get("capital", 0)
            t.add_row(
                "",
                f"[bold red]⚠️  CLOSE_FAILED — posición sin cerrar en Polymarket[/bold red]",
            )
            t.add_row(
                "",
                f"[bold red]   Token: {_tid}...  │  Capital en riesgo: ${_cap:.2f}[/bold red]",
            )
            t.add_row(
                "",
                "[bold red]   Verificar manualmente en polymarket.com[/bold red]",
            )
    elif live_trader.has_close_failed:
        t.add_row(
            "",
            "[bold yellow]⚠️  Cierre fallido anterior — revisar live_trades.json[/bold yellow]",
        )

    # ── Open position or idle ────────────────────────────────────────────
    pm_up    = ds.get("pm_up")
    open_pos = live_trader.current_open_position

    if open_pos:
        direction = open_pos["direction"]
        d_col     = "green" if direction == "LONG" else "red"
        coin_tf   = f"{open_pos['coin']} {open_pos['timeframe']}"
        e_det     = open_pos["entry_price_detected"]
        e_real    = open_pos["entry_price_real"]
        slip_pct2 = open_pos.get("slippage_applied", 0) * 100
        tp        = open_pos["tp_target"]
        sl        = open_pos["sl_target"]
        highest   = open_pos.get("highest_price", e_real)

        t.add_row("", "")
        t.add_row(
            "POSICIÓN ABIERTA",
            f"[{d_col} bold]{direction}[/{d_col} bold]  {coin_tf}",
        )
        t.add_row(
            "Detectado / Fill / Slip",
            f"${e_det:.4f}  │  ${e_real:.4f}  │  {slip_pct2:+.2f}%",
        )
        trail_str = ""
        if highest >= 0.75:
            trail_stop = round(highest - 0.12, 3)
            trail_str  = f"  │  [yellow]Trail activo: ${trail_stop:.3f}[/yellow]"
        t.add_row(
            "TP / SL",
            f"[green]TP: ${tp:.3f}[/green]  │  [red]SL: ${sl:.3f}[/red]{trail_str}",
        )

        if pm_up is not None:
            cur_contract = pm_up if direction == "LONG" else 1.0 - pm_up
            upnl         = live_trader.unrealized_pnl(pm_up)
            u_col        = "green" if (upnl or 0) >= 0 else "red"
            arrow        = "↑" if (upnl or 0) >= 0 else "↓"
            upnl_s       = f"{'+' if (upnl or 0) >= 0 else ''}${upnl:.2f}" if upnl is not None else "—"
            t.add_row(
                "Actual / P&L flotante",
                f"${cur_contract:.4f}  │  [{u_col} bold]{upnl_s} {arrow}[/{u_col} bold]",
            )
    else:
        t.add_row("", "")
        t.add_row("Estado", "[dim]Sin posición abierta — esperando señal[/dim]")

    return Panel(t, title="LIVE TRADING", box=bx.ROUNDED, expand=True)


def render(
    ds: dict,
    trader: "pt.PaperTrader | None" = None,
    live_trader=None,
) -> "_Group":
    """Build the full dashboard layout from pre-computed state dict."""
    header = _header(ds)

    grid = Table(box=None, pad_edge=False, show_header=False, expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(
        Group(_ob_panel(ds), _ta_panel(ds)),
        _flow_panel(ds),
    )

    panels: list = [header, grid, _signals_panel(ds)]
    if trader is not None:
        panels.append(_paper_panel(trader, ds))
    if live_trader is not None:
        panels.append(_live_panel(live_trader, ds))

    return _Group(*panels)

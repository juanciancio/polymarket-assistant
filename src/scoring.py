import indicators as ind


def detect_divergence(pm_up_price: float | None, score: int) -> dict:
    """Compare Polymarket Up price against the technical entry score.

    Args:
        pm_up_price: best-ask price of the PM Up contract (0-1), or None if unavailable.
        score:       result of calculate_entry_score()["score"].

    Returns:
        {
            "has_divergence"  : bool,
            "conviction_level": str,   # "MAX_BULLISH"|"MAX_BEARISH"|"DIVERGENCE"|"NEUTRAL"
            "color"           : str,   # Rich color name
            "message"         : str,
        }
    """
    _neutral = {"has_divergence": False, "conviction_level": "NEUTRAL",
                "color": "white", "message": "No conviction signal"}

    if pm_up_price is None:
        return _neutral

    pm = pm_up_price  # shorthand, range 0-1 (e.g. 0.72 = 72%)

    if pm > 0.65 and score >= 4:
        return {"has_divergence": False, "conviction_level": "MAX_BULLISH",
                "color": "green",
                "message": f"PM UP {pm:.0%} + score {score:+d} — máxima convicción alcista"}

    if pm < 0.35 and score <= -4:
        return {"has_divergence": False, "conviction_level": "MAX_BEARISH",
                "color": "red",
                "message": f"PM UP {pm:.0%} + score {score:+d} — máxima convicción bajista"}

    if pm > 0.65 and score <= -3:
        return {"has_divergence": True, "conviction_level": "DIVERGENCE",
                "color": "yellow",
                "message": f"PM UP {pm:.0%} pero score {score:+d} — Polymarket vs técnicos"}

    if pm < 0.35 and score >= 3:
        return {"has_divergence": True, "conviction_level": "DIVERGENCE",
                "color": "yellow",
                "message": f"PM UP {pm:.0%} pero score {score:+d} — Polymarket vs técnicos"}

    return _neutral


def calculate_entry_score(state) -> dict:
    """Evaluate current market conditions and return an entry scoring result.

    Args:
        state: feeds.State instance with current bids/asks/mid/trades/klines.

    Returns:
        {
            "score"               : int,   # total score
            "direction"           : str,   # "BULLISH" | "BEARISH" | "NEUTRAL"
            "triggered_conditions": list,  # [(label, pts), ...]
            "entry_signal"        : bool,  # True only when |score| >= 4
        }
    """
    _empty = {"score": 0, "direction": "NEUTRAL", "triggered_conditions": [], "entry_signal": False}

    if not state.mid or not state.klines:
        return _empty

    bids, asks, mid = state.bids, state.asks, state.mid
    trades, klines  = state.trades, state.klines

    score     = 0
    triggered = []

    bias  = ind.bias_score(bids, asks, mid, trades, klines)  # [-100, +100]
    cvd5  = ind.cvd(trades, 300)
    cvd3  = ind.cvd(trades, 180)
    vwap_v = ind.vwap(klines)
    obi_v  = ind.obi(bids, asks, mid)                        # [-1, +1]
    ema_s, ema_l = ind.emas(klines)
    ha     = ind.heikin_ashi(klines)

    # ── BULLISH conditions ───────────────────────────────────────
    if bias > 65:
        score += 2
        triggered.append(("BIAS > 65%", +2))

    if cvd5 > 0 and cvd3 > 0:
        score += 1
        triggered.append(("CVD 5m positive & growing", +1))

    if vwap_v and mid > vwap_v:
        score += 1
        triggered.append(("Price above VWAP", +1))

    if obi_v > 0:
        score += 1
        triggered.append(("OBI positive", +1))

    if ema_s is not None and ema_l is not None and ema_s > ema_l:
        score += 1
        triggered.append(("EMA 5 > EMA 20 (golden cross)", +1))

    if len(ha) >= 3 and all(c["green"] for c in ha[-3:]):
        score += 1
        triggered.append(("HA 3+ bullish candles", +1))

    # ── BEARISH conditions ───────────────────────────────────────
    if bias < -45:
        score -= 2
        triggered.append(("BIAS < -45%", -2))

    if cvd5 < 0:
        score -= 1
        triggered.append(("CVD 5m negative", -1))

    if vwap_v and mid < vwap_v:
        score -= 1
        triggered.append(("Price below VWAP", -1))

    if obi_v < -0.30:
        score -= 1
        triggered.append(("OBI < -30%", -1))

    if ema_s is not None and ema_l is not None and ema_s < ema_l:
        score -= 1
        triggered.append(("EMA 5 < EMA 20 (death cross)", -1))

    if len(ha) >= 3 and all(not c["green"] for c in ha[-3:]):
        score -= 1
        triggered.append(("HA 3+ bearish candles", -1))

    # ── Thresholds ───────────────────────────────────────────────
    if score >= 4:
        direction    = "BULLISH"
        entry_signal = True
    elif score <= -4:
        direction    = "BEARISH"
        entry_signal = True
    else:
        direction    = "NEUTRAL"
        entry_signal = False

    return {
        "score":                score,
        "direction":            direction,
        "triggered_conditions": triggered,
        "entry_signal":         entry_signal,
    }

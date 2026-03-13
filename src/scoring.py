import indicators as ind

# Entry thresholds
LONG_THRESHOLD  = 5   # score >= +5 required for BULLISH entry
SHORT_THRESHOLD = -5  # score <= -5 required for BEARISH entry (stricter than LONG)


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

    if pm > 0.65 and score >= LONG_THRESHOLD:
        return {"has_divergence": False, "conviction_level": "MAX_BULLISH",
                "color": "green",
                "message": f"PM UP {pm:.0%} + score {score:+d} — máxima convicción alcista"}

    if pm < 0.35 and score <= SHORT_THRESHOLD:
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


def calculate_entry_score(state, precomputed: dict | None = None) -> dict:
    """Evaluate current market conditions and return an entry scoring result.

    LONG  entry requires score >= +5  (raised from +4 to filter weak entries).
    SHORT entry requires score <= -5  (stricter filter to reduce false SHORT signals).

    Args:
        state: feeds.State instance with current bids/asks/mid/trades/klines.

    Returns:
        {
            "score"               : int,   # total score
            "direction"           : str,   # "BULLISH" | "BEARISH" | "NEUTRAL"
            "triggered_conditions": list,  # [(label, pts), ...]
            "entry_signal"        : bool,
            "entry_threshold_used": int,   # +4 for LONG, -5 for SHORT, 0 for NEUTRAL
        }
    """
    _empty = {
        "score": 0, "direction": "NEUTRAL", "triggered_conditions": [],
        "entry_signal": False, "entry_threshold_used": 0,
    }

    if not state.mid or not state.klines:
        return _empty

    bids, asks, mid = state.bids, state.asks, state.mid
    trades, klines  = state.trades, state.klines

    score     = 0
    triggered = []

    if precomputed is not None:
        bias   = precomputed["bias"]
        cvd5   = precomputed["cvd5"]
        cvd3   = precomputed["cvd3"]
        vwap_v = precomputed["vwap"]
        obi_v  = precomputed["obi"]
        ema_s  = precomputed["ema_s"]
        ema_l  = precomputed["ema_l"]
        ha     = precomputed["ha"]
    else:
        bias   = ind.bias_score(bids, asks, mid, trades, klines)  # [-100, +100]
        cvd5   = ind.cvd(trades, 300)
        cvd3   = ind.cvd(trades, 180)
        vwap_v = ind.vwap(klines)
        obi_v  = ind.obi(bids, asks, mid)                         # [-1, +1]
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

    # ── Thresholds (asymmetric) ──────────────────────────────────
    if score >= LONG_THRESHOLD:
        direction    = "BULLISH"
        entry_signal = True
        threshold    = LONG_THRESHOLD
    elif score <= SHORT_THRESHOLD:
        direction    = "BEARISH"
        entry_signal = True
        threshold    = SHORT_THRESHOLD
    else:
        direction    = "NEUTRAL"
        entry_signal = False
        threshold    = 0

    return {
        "score":                score,
        "direction":            direction,
        "triggered_conditions": triggered,
        "entry_signal":         entry_signal,
        "entry_threshold_used": threshold,
    }

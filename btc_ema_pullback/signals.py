"""Pure signal generation (1h EMA trend + 15m EMA20 pullback) on *closed* candles.

No I/O here: the same functions run on live candles and on historical klines
for backtests.  Candle rows are ``[open_ms, open, high, low, close, volume]``.
"""

from __future__ import annotations

TIMEFRAME_MS = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000}


def closed_candles(rows, timeframe, now_ms):
    """Drop the in-progress candle (and anything malformed)."""
    span = TIMEFRAME_MS[timeframe]
    result = []
    for row in rows or []:
        try:
            open_ms = int(row[0])
            values = [float(row[i]) for i in range(1, 5)]
        except (TypeError, ValueError, IndexError):
            continue
        if open_ms + span <= int(now_ms) and min(values) > 0:
            result.append([open_ms, *values, float(row[5]) if len(row) > 5 else 0.0])
    result.sort(key=lambda r: r[0])
    return result


def ema(values, period):
    """EMA seeded with the SMA of the first ``period`` values (TradingView style)."""
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out = [None] * (period - 1) + [seed]
    current = seed
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
        out.append(current)
    return out


def atr(rows, period):
    """Wilder ATR aligned with ``rows`` (None until enough history)."""
    if len(rows) <= period:
        return [None] * len(rows)
    trs = [rows[0][2] - rows[0][3]]
    for prev, row in zip(rows, rows[1:]):
        trs.append(max(row[2] - row[3], abs(row[2] - prev[4]), abs(row[3] - prev[4])))
    out = [None] * period
    current = sum(trs[1 : period + 1]) / period
    out.append(current)
    for tr in trs[period + 1 :]:
        current = (current * (period - 1) + tr) / period
        out.append(current)
    return out


def count_crosses(fast, slow, lookback):
    diffs = [f - s for f, s in zip(fast[-lookback:], slow[-lookback:]) if f is not None and s is not None]
    crosses = 0
    for prev, cur in zip(diffs, diffs[1:]):
        if (prev > 0) != (cur > 0) and prev != 0 and cur != 0:
            crosses += 1
    return crosses


def evaluate_trend(trend_rows, cfg):
    """1h trend + sideways filter.  Returns direction LONG/SHORT or a skip reason."""
    fast_p, slow_p = int(cfg["ema_fast"]), int(cfg["ema_slow"])
    slope_k = int(cfg["ema_slope_lookback"])
    cross_k = int(cfg["recent_cross_lookback"])
    need = slow_p + max(slope_k, cross_k) + 2
    if len(trend_rows) < need:
        return {"direction": None, "skip_reason": f"INSUFFICIENT_1H_CANDLES:{len(trend_rows)}<{need}"}
    closes = [r[4] for r in trend_rows]
    e_fast, e_slow = ema(closes, fast_p), ema(closes, slow_p)
    close, f_now, s_now = closes[-1], e_fast[-1], e_slow[-1]
    slope = (e_fast[-1] - e_fast[-1 - slope_k]) / slope_k / close
    slow_slope = (e_slow[-1] - e_slow[-1 - slope_k]) / slope_k / close
    separation = abs(f_now - s_now) / close
    crosses = count_crosses(e_fast, e_slow, cross_k)
    info = {
        "1h_bar_open_ms": trend_rows[-1][0],
        "1h_close": close,
        "1h_ema20": f_now,
        "1h_ema50": s_now,
        "1h_ema20_prev": e_fast[-2],
        "1h_ema20_slope": slope,
        "1h_ema50_slope": slow_slope,
        "1h_ema_separation": separation,
        "1h_recent_crosses": crosses,
    }
    if close > f_now > s_now and e_fast[-1] > e_fast[-2]:
        direction = "LONG"
    elif close < f_now < s_now and e_fast[-1] < e_fast[-2]:
        direction = "SHORT"
    else:
        return {**info, "direction": None, "skip_reason": "NO_1H_TREND"}
    if separation < float(cfg["ema_separation_threshold"]):
        return {**info, "direction": None, "skip_reason": "CHOP_EMA_TOO_CLOSE"}
    signed_slope = slope if direction == "LONG" else -slope
    if signed_slope < float(cfg["ema_slope_threshold"]):
        return {**info, "direction": None, "skip_reason": "CHOP_EMA_FLAT"}
    if crosses > int(cfg["max_recent_crosses"]):
        return {**info, "direction": None, "skip_reason": "CHOP_RECENT_CROSSES"}
    return {**info, "direction": direction, "skip_reason": ""}


def evaluate_entry(entry_rows, direction, cfg):
    """15m pullback to EMA20 + confirmation breakout on the last closed candle."""
    fast_p, slow_p = int(cfg["ema_fast"]), int(cfg["ema_slow"])
    atr_p = int(cfg["atr_period"])
    lookback = int(cfg["pullback_lookback"])
    need = slow_p + atr_p + lookback + 3
    if len(entry_rows) < need:
        return {"entry_condition": False, "skip_reason": f"INSUFFICIENT_15M_CANDLES:{len(entry_rows)}<{need}"}
    closes = [r[4] for r in entry_rows]
    e_fast, e_slow = ema(closes, fast_p), ema(closes, slow_p)
    atrs = atr(entry_rows, atr_p)
    bar, prev = entry_rows[-1], entry_rows[-2]
    base_atr = atrs[-2] or atrs[-1]
    o, h, l, c = bar[1], bar[2], bar[3], bar[4]
    candle_range, body = h - l, abs(c - o)
    tol = float(cfg["pullback_tolerance_pct"])
    brk = float(cfg["ema50_break_tolerance_pct"])
    window = range(len(entry_rows) - lookback - 1, len(entry_rows))
    long = direction == "LONG"
    info = {
        "15m_bar_open_ms": bar[0],
        "15m_open": o,
        "15m_high": h,
        "15m_low": l,
        "15m_close": c,
        "15m_ema20": e_fast[-1],
        "15m_ema50": e_slow[-1],
        "previous_high_or_low": prev[2] if long else prev[3],
        "atr": base_atr,
        "candle_range": candle_range,
        "candle_body": body,
    }
    if long:
        checks = {
            "15m_uptrend": e_fast[-1] > e_slow[-1],
            "pullback_to_ema20": any(entry_rows[i][3] <= e_fast[i] * (1 + tol) for i in window),
            "no_ema50_breakdown": all(entry_rows[i][4] >= e_slow[i] * (1 - brk) for i in window),
            "confirmation_candle": c > o and c > e_fast[-1],
            "breaks_previous_high": c > prev[2],
        }
    else:
        checks = {
            "15m_downtrend": e_fast[-1] < e_slow[-1],
            "pullback_to_ema20": any(entry_rows[i][2] >= e_fast[i] * (1 - tol) for i in window),
            "no_ema50_breakout": all(entry_rows[i][4] <= e_slow[i] * (1 + brk) for i in window),
            "confirmation_candle": c < o and c < e_fast[-1],
            "breaks_previous_low": c < prev[3],
        }
    checks["not_chasing_range"] = bool(base_atr) and candle_range <= float(cfg["max_entry_candle_atr_multiple"]) * base_atr
    checks["not_chasing_body"] = bool(base_atr) and body <= float(cfg["max_entry_body_atr_multiple"]) * base_atr
    failed = [name for name, ok in checks.items() if not ok]
    return {
        **info,
        "checks": checks,
        "entry_condition": not failed,
        "skip_reason": "" if not failed else "15M_" + "+".join(failed).upper(),
    }


def generate_signal(trend_rows, entry_rows, cfg, now_ms, symbol="BTCUSDT"):
    """Return one structured decision for the latest closed 15m candle."""
    trend_closed = closed_candles(trend_rows, "1h", now_ms)
    entry_closed = closed_candles(entry_rows, "15m", now_ms)
    trend = evaluate_trend(trend_closed, cfg)
    result = {"symbol": symbol, "timestamp_ms": int(now_ms), **trend, "side": None, "entry_condition": False}
    if entry_closed:
        bar_open = entry_closed[-1][0]
        result["15m_bar_open_ms"] = bar_open
        result["signal_age_seconds"] = (int(now_ms) - bar_open - TIMEFRAME_MS["15m"]) / 1000.0
    if not trend.get("direction"):
        return result
    entry = evaluate_entry(entry_closed, trend["direction"], cfg)
    result.update(entry)
    if not entry.get("entry_condition"):
        return result
    if result.get("signal_age_seconds", 1e9) > float(cfg["signal_max_age_seconds"]):
        result["entry_condition"] = False
        result["skip_reason"] = "SIGNAL_STALE"
        return result
    result["side"] = trend["direction"]
    result["signal_id"] = f"{symbol}:{trend['direction']}:{entry['15m_bar_open_ms']}"
    return result


__all__ = (
    "TIMEFRAME_MS",
    "atr",
    "closed_candles",
    "count_crosses",
    "ema",
    "evaluate_entry",
    "evaluate_trend",
    "generate_signal",
)

"""Indicator helpers shared by UT Breakout live code and tests."""

from math import isfinite, sqrt


def _as_float_list(values):
    result = []
    for value in values or []:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float("nan")
        result.append(parsed)
    return result


def _finite_float_list(values):
    return [value for value in _as_float_list(values) if isfinite(value)]


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _std(values):
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    return sqrt(sum((item - avg) ** 2 for item in values) / len(values))


def rolling_percentile_rank(values, lookback):
    clean = _finite_float_list(values)
    try:
        lookback = int(lookback or 0)
    except (TypeError, ValueError):
        lookback = 0
    if lookback <= 1:
        return {"ready": False, "reason": "invalid_lookback", "percentile": None, "samples": len(clean)}
    if len(clean) < lookback:
        return {
            "ready": False,
            "reason": "insufficient_data",
            "percentile": None,
            "samples": len(clean),
            "lookback": lookback,
        }

    window = clean[-lookback:]
    current = window[-1]
    below = sum(1 for item in window if item < current)
    equal = sum(1 for item in window if item == current)
    percentile = (below + 0.5 * equal) / len(window) * 100.0
    return {
        "ready": True,
        "reason": "OK",
        "value": current,
        "percentile": percentile,
        "samples": len(window),
        "lookback": lookback,
    }


def bollinger_width_percentile(close_values, length=20, mult=2.0, lookback=80):
    closes = _finite_float_list(close_values)
    try:
        length = int(length or 20)
        lookback = int(lookback or 80)
        mult = float(mult or 2.0)
    except (TypeError, ValueError):
        return {"ready": False, "reason": "invalid_params", "percentile": None}
    if length < 2 or lookback < 2 or mult <= 0:
        return {"ready": False, "reason": "invalid_params", "percentile": None}
    required = length + lookback - 1
    if len(closes) < required:
        return {
            "ready": False,
            "reason": "insufficient_data",
            "percentile": None,
            "samples": max(0, len(closes) - length + 1),
            "required": required,
        }

    widths = []
    for idx in range(length - 1, len(closes)):
        window = closes[idx - length + 1:idx + 1]
        basis = _mean(window)
        std = _std(window)
        if basis == 0.0:
            continue
        upper = basis + (std * mult)
        lower = basis - (std * mult)
        width_pct = (upper - lower) / abs(basis) * 100.0
        if isfinite(width_pct):
            widths.append(width_pct)

    rank = rolling_percentile_rank(widths, lookback)
    if not rank.get("ready"):
        rank.update({"width_pct": None})
        return rank
    rank["width_pct"] = rank.get("value")
    return rank


def _true_ranges(highs, lows, closes):
    ranges = []
    for idx, high in enumerate(highs):
        low = lows[idx]
        if idx == 0:
            ranges.append(high - low)
            continue
        prev_close = closes[idx - 1]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return ranges


def keltner_squeeze_state(
    high_values,
    low_values,
    close_values,
    bb_length=20,
    bb_mult=2.0,
    kc_length=20,
    kc_mult=1.5,
):
    highs = _finite_float_list(high_values)
    lows = _finite_float_list(low_values)
    closes = _finite_float_list(close_values)
    size = min(len(highs), len(lows), len(closes))
    highs = highs[-size:]
    lows = lows[-size:]
    closes = closes[-size:]
    try:
        bb_length = int(bb_length or 20)
        kc_length = int(kc_length or 20)
        bb_mult = float(bb_mult or 2.0)
        kc_mult = float(kc_mult or 1.5)
    except (TypeError, ValueError):
        return {"ready": False, "reason": "invalid_params"}
    if min(bb_length, kc_length) < 2 or bb_mult <= 0 or kc_mult <= 0:
        return {"ready": False, "reason": "invalid_params"}
    required = max(bb_length, kc_length) + 1
    if size < required:
        return {"ready": False, "reason": "insufficient_data", "samples": size, "required": required}

    bb_window = closes[-bb_length:]
    bb_mid = _mean(bb_window)
    bb_std = _std(bb_window)
    bb_upper = bb_mid + (bb_std * bb_mult)
    bb_lower = bb_mid - (bb_std * bb_mult)

    kc_close = closes[-kc_length:]
    kc_mid = _mean(kc_close)
    atr_values = _true_ranges(highs, lows, closes)[-kc_length:]
    atr = _mean(atr_values)
    kc_upper = kc_mid + (atr * kc_mult)
    kc_lower = kc_mid - (atr * kc_mult)
    bb_width_pct = (bb_upper - bb_lower) / max(abs(bb_mid), 1e-9) * 100.0
    kc_width_pct = (kc_upper - kc_lower) / max(abs(kc_mid), 1e-9) * 100.0
    squeeze_on = bb_upper <= kc_upper and bb_lower >= kc_lower
    return {
        "ready": True,
        "reason": "OK",
        "squeeze_on": bool(squeeze_on),
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "kc_upper": kc_upper,
        "kc_lower": kc_lower,
        "bb_width_pct": bb_width_pct,
        "kc_width_pct": kc_width_pct,
        "atr": atr,
        "samples": size,
    }


def previous_donchian(high_values, low_values, length):
    """Return Donchian values from the previous completed candles only.

    The final item in high_values/low_values is treated as the current decision
    candle and is intentionally excluded.
    """
    length = int(length or 0)
    highs = _as_float_list(high_values)
    lows = _as_float_list(low_values)
    if length <= 0:
        raise ValueError("length must be positive")
    if len(highs) != len(lows):
        raise ValueError("high_values and low_values must have the same length")
    if len(highs) < length + 1:
        return {
            "ready": False,
            "high": None,
            "low": None,
            "width": None,
            "reason": f"need at least {length + 1} candles",
        }

    window_highs = highs[-length - 1:-1]
    window_lows = lows[-length - 1:-1]
    if any(not isfinite(v) for v in window_highs + window_lows):
        return {
            "ready": False,
            "high": None,
            "low": None,
            "width": None,
            "reason": "window contains non-finite values",
        }

    high = max(window_highs)
    low = min(window_lows)
    return {
        "ready": True,
        "high": high,
        "low": low,
        "width": high - low,
        "reason": "ok",
    }

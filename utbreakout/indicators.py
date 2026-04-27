"""Indicator helpers shared by UT Breakout live code and tests."""

from math import isfinite


def _as_float_list(values):
    result = []
    for value in values or []:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float("nan")
        result.append(parsed)
    return result


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

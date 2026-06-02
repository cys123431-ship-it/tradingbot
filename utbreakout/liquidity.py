"""Liquidity and candle-structure helpers for UT Breakout.

The helpers are deliberately pure and can be reused by live code and
backtests without touching exchange state.
"""

from math import isfinite


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _bar_value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def candle_range(row):
    high = _finite_float(_bar_value(row, "high"))
    low = _finite_float(_bar_value(row, "low"))
    return max(high - low, 0.0)


def upper_wick_ratio(row):
    high = _finite_float(_bar_value(row, "high"))
    low = _finite_float(_bar_value(row, "low"))
    open_price = _finite_float(_bar_value(row, "open"))
    close = _finite_float(_bar_value(row, "close"))
    total = max(high - low, 1e-9)
    wick = max(0.0, high - max(open_price, close))
    return wick / total


def lower_wick_ratio(row):
    high = _finite_float(_bar_value(row, "high"))
    low = _finite_float(_bar_value(row, "low"))
    open_price = _finite_float(_bar_value(row, "open"))
    close = _finite_float(_bar_value(row, "close"))
    total = max(high - low, 1e-9)
    wick = max(0.0, min(open_price, close) - low)
    return wick / total


def build_pivot_levels(row):
    high = _finite_float(_bar_value(row, "high"))
    low = _finite_float(_bar_value(row, "low"))
    close = _finite_float(_bar_value(row, "close"))
    pivot = (high + low + close) / 3.0 if high > 0 and low > 0 and close > 0 else 0.0
    return {
        "pivot": pivot,
        "r1": 2.0 * pivot - low,
        "s1": 2.0 * pivot - high,
        "r2": pivot + (high - low),
        "s2": pivot - (high - low),
    }


def nearest_levels(rows, idx, lookback=20):
    """Return simple public-data support/resistance levels.

    This intentionally avoids predictive fitting. It only exposes recent highs,
    lows, and a pivot map as context for downstream engines.
    """
    rows = list(rows or [])
    if not rows:
        return {"support_levels": [], "resistance_levels": [], "pivot_levels": {}}
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = len(rows) - 1
    idx = max(0, min(idx, len(rows) - 1))
    start = max(0, idx - max(1, int(lookback or 20)))
    sample = rows[start:idx] or rows[max(0, idx - 1):idx + 1]
    supports = sorted({_finite_float(_bar_value(row, "low")) for row in sample if _finite_float(_bar_value(row, "low")) > 0})
    resistances = sorted(
        {_finite_float(_bar_value(row, "high")) for row in sample if _finite_float(_bar_value(row, "high")) > 0},
        reverse=True,
    )
    return {
        "support_levels": supports[:3],
        "resistance_levels": resistances[:3],
        "pivot_levels": build_pivot_levels(rows[idx]),
    }


def bad_liquidity(context=None, config=None):
    config = config or {}
    spread_bps = _finite_float(
        context.get("spread_bps") if isinstance(context, dict) else getattr(context, "spread_bps", None),
        None,
    )
    if spread_bps is None:
        return False
    return spread_bps > _finite_float(config.get("max_spread_bps"), 10.0)

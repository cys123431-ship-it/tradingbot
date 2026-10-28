"""SMA3/SMA120 cross with selectable A~E feature blocks (pure, backtestable).

The signal itself is the SMA3/SMA120 cross (or, with feature E, an N-bar
Donchian breakout).  Features A~E are independent switches so every single
feature and every combination can be compared against the others.
"""

from __future__ import annotations

from .config import feature_mode
from .indicators import (
    adx,
    atr,
    chandelier_stop,
    donchian,
    sma,
    trend_efficiency,
    volatility_target_scale,
)

CROSS_LONG = "LONG"
CROSS_SHORT = "SHORT"
TRIGGER_SMA = "SMA_CROSS"
TRIGGER_DONCHIAN = "DONCHIAN"


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _aligned(values, length):
    """Pad indicator output so it lines up with ``rows`` (leading Nones)."""
    if not values:
        return [None] * length
    if len(values) == length:
        return values
    missing = length - len(values)
    return [None] * max(0, missing) + list(values)[-length:]


def indicator_frame(rows, cfg):
    """All indicators the features need, aligned with ``rows``."""
    closes = [float(r[4]) for r in rows]
    length = len(rows)
    fast = _aligned(sma(closes, int(cfg["fast_period"])), length)
    slow = _aligned(sma(closes, int(cfg["slow_period"])), length)
    atr_values = _aligned(atr(rows, 14), length)
    frame = {"closes": closes, "fast": fast, "slow": slow, "atr": atr_values}
    mode = feature_mode(cfg)
    if "A" in mode:
        frame["adx"] = _aligned(adx(rows, 14), length)
        frame["efficiency"] = _aligned(trend_efficiency(closes, 20), length)
    if "E" in mode:
        upper, lower = donchian(rows, int(cfg["feature_e_period"]))
        frame["donchian_upper"] = upper
        frame["donchian_lower"] = lower
    return frame


def cross_side(frame, index):
    """(side, trigger) of a fresh cross / breakout at ``index``, else (None, None)."""
    fast, slow = frame["fast"], frame["slow"]
    if index < 1 or index >= len(fast):
        return None, None
    if fast[index] is None or slow[index] is None or fast[index - 1] is None or slow[index - 1] is None:
        return None, None
    if fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]:
        return CROSS_LONG, TRIGGER_SMA
    if fast[index - 1] >= slow[index - 1] and fast[index] < slow[index]:
        return CROSS_SHORT, TRIGGER_SMA
    upper = frame.get("donchian_upper")
    lower = frame.get("donchian_lower")
    if upper is not None and lower is not None:
        close = frame["closes"][index]
        if upper[index] is not None and close > upper[index]:
            return CROSS_LONG, TRIGGER_DONCHIAN
        if lower[index] is not None and close < lower[index]:
            return CROSS_SHORT, TRIGGER_DONCHIAN
    return None, None


def trend_filter_ok(frame, index, side, cfg):
    """Feature A: SMA120 side must agree and the market must not be chopping."""
    slow = frame["slow"][index]
    close = frame["closes"][index]
    if slow is None:
        return False, "NO_SLOW_SMA"
    if side == CROSS_LONG and close <= slow:
        return False, "REJECTED_TREND_FILTER_SLOW_SMA"
    if side == CROSS_SHORT and close >= slow:
        return False, "REJECTED_TREND_FILTER_SLOW_SMA"
    adx_values = frame.get("adx") or []
    efficiency_values = frame.get("efficiency") or []
    adx_value = adx_values[index] if index < len(adx_values) else None
    efficiency = efficiency_values[index] if index < len(efficiency_values) else None
    if adx_value is None or efficiency is None:
        return False, "REJECTED_TREND_FILTER_NO_DATA"
    if adx_value < float(cfg["feature_a_adx_min"]):
        return False, "REJECTED_TREND_FILTER_LOW_ADX"
    if efficiency < float(cfg["feature_a_efficiency_min"]):
        return False, "REJECTED_TREND_FILTER_LOW_EFFICIENCY"
    return True, ""


def initial_stop(side, entry_price, atr_value, cfg):
    """Feature B: ATR structural stop.  Returns (price, distance) or (None, None)."""
    entry = _f(entry_price)
    atr_val = _f(atr_value)
    if entry is None or entry <= 0 or atr_val is None or atr_val <= 0:
        return None, None
    distance = atr_val * float(cfg["feature_b_atr_multiplier"])
    price = entry - distance if side == CROSS_LONG else entry + distance
    if price <= 0:
        return None, None
    return price, distance


def size_multiplier(atr_value, close, cfg):
    """Feature C: volatility-target sizing multiplier (1.0 when disabled)."""
    if not cfg.get("feature_c_vol_sizing"):
        return 1.0
    return volatility_target_scale(
        atr_value,
        close,
        float(cfg["feature_c_target_atr_pct"]),
        float(cfg["feature_c_min_scale"]),
        float(cfg["feature_c_max_scale"]),
    )


def trailing_stop(side, trade, price, atr_value, cfg):
    """Feature D: chandelier stop for a held trade (never loosens)."""
    if not cfg.get("feature_d_chandelier"):
        return None
    highest = _f(trade.get("highest_price"), price)
    lowest = _f(trade.get("lowest_price"), price)
    if side == CROSS_LONG:
        highest = max(highest or price, price)
        trade["highest_price"] = str(highest)
    else:
        lowest = min(lowest or price, price)
        trade["lowest_price"] = str(lowest)
    entry = _f(trade.get("entry_price"))
    previous = _f(trade.get("trail_price"))
    return chandelier_stop(
        side,
        highest,
        lowest,
        atr_value,
        float(cfg["feature_d_atr_multiplier"]),
        previous=previous,
        breakeven=entry,
    )


def evaluate(rows, cfg, index=None, frame=None):
    """Decision for the candle at ``index`` (default: last closed candle).

    ``frame`` may be passed to reuse precomputed indicators; the backtest does
    this so a long series is not re-scanned once per candle.
    """
    mode = feature_mode(cfg)
    result = {"side": None, "trigger": None, "skip_reason": "", "features": mode,
              "lock_enabled": bool(cfg.get("locking", True)), "atr": None,
              "size_multiplier": 1.0, "initial_stop": None, "atr_distance": None}
    if not mode:
        result["skip_reason"] = "NO_FEATURE_SELECTED"
        return result
    if not rows:
        result["skip_reason"] = "NO_DATA"
        return result
    index = (len(rows) - 1) if index is None else int(index)
    if index < 1 or index >= len(rows):
        result["skip_reason"] = "NO_DATA"
        return result
    if frame is None:
        frame = indicator_frame(rows, cfg)
    side, trigger = cross_side(frame, index)
    atr_value = frame["atr"][index]
    result["atr"] = atr_value
    if side is None:
        result["skip_reason"] = "NO_SIGNAL"
        return result
    if "A" in mode:
        ok, reason = trend_filter_ok(frame, index, side, cfg)
        if not ok:
            result["skip_reason"] = reason
            return result
    close = frame["closes"][index]
    result.update(side=side, trigger=trigger, skip_reason="")
    result["size_multiplier"] = size_multiplier(atr_value, close, cfg)
    if "B" in mode:
        stop_price, distance = initial_stop(side, close, atr_value, cfg)
        result["initial_stop"] = stop_price
        result["atr_distance"] = distance
        if stop_price is None:
            result["skip_reason"] = "REJECTED_NO_ATR_FOR_STOP"
            result.update(side=None)
    return result

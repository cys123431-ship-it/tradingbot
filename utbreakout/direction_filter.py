"""Direction filter for UT Breakout.

This module intentionally keeps the long/short gate simple.

Design:
- BTC higher timeframe determines broad market regime.
- Symbol 1h determines symbol-side allowance.
- 15m UTBot remains the main entry trigger in emas.py.
- Non-critical filters should reduce size, not block trades.

No exchange access, no secrets, no order placement here.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping


@dataclass(frozen=True)
class DirectionDecision:
    long_allowed: bool
    short_allowed: bool
    regime: str
    reason: str
    size_multiplier: float


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _slope_value(metrics: Mapping[str, Any], *names: str) -> float:
    for name in names:
        value = _f(metrics.get(name))
        if value is not None:
            return value
    return 0.0


def _close_ema_bias(metrics: Mapping[str, Any]) -> str:
    """Return bullish/bearish/neutral using common metric keys.

    This accepts several key names because the existing bot has many diagnostic
    dictionaries with slightly different names.
    """
    close = _f(metrics.get("close") or metrics.get("last") or metrics.get("price"))
    ema_fast = _f(
        metrics.get("ema_fast")
        or metrics.get("ema_20")
        or metrics.get("ema20")
        or metrics.get("ema_short")
    )
    ema_slow = _f(
        metrics.get("ema_slow")
        or metrics.get("ema_50")
        or metrics.get("ema50")
        or metrics.get("ema_long")
    )
    ema_slope = _slope_value(
        metrics,
        "ema_slope",
        "ema_slow_slope",
        "ema50_slope",
        "ema_trend_slope",
    )

    if close is None or ema_slow is None:
        return "neutral"

    if ema_fast is not None:
        if close >= ema_fast >= ema_slow and ema_slope >= -0.02:
            return "bullish"
        if close <= ema_fast <= ema_slow and ema_slope <= 0.02:
            return "bearish"

    if close > ema_slow and ema_slope >= -0.02:
        return "bullish"
    if close < ema_slow and ema_slope <= 0.02:
        return "bearish"
    return "neutral"


def _volume_multiplier(metrics: Mapping[str, Any]) -> tuple[float, str]:
    """Volume is a sizing input, not a hard block except in extreme cases."""
    vr = _f(metrics.get("volume_ratio"), 1.0)
    if vr is None:
        return 0.85, "volume unknown"
    if vr < 0.35:
        return 0.0, f"volume extremely weak {vr:.2f}<0.35"
    if vr < 0.55:
        return 0.50, f"volume weak {vr:.2f}"
    if vr < 0.75:
        return 0.75, f"volume moderate {vr:.2f}"
    return 1.0, f"volume ok {vr:.2f}"


def _quality_multiplier(metrics: Mapping[str, Any]) -> tuple[float, str]:
    """Quality score should reduce size before it blocks a trade."""
    score = _f(
        metrics.get("quality_score")
        or metrics.get("quality_score_v2")
        or metrics.get("strategy_quality")
        or metrics.get("trend_health")
    )
    if score is None:
        return 0.85, "quality unknown"
    if score < 20:
        return 0.0, f"quality too low {score:.1f}<20"
    if score < 40:
        return 0.50, f"quality weak {score:.1f}"
    if score < 55:
        return 0.75, f"quality moderate {score:.1f}"
    return 1.0, f"quality ok {score:.1f}"


def decide_direction(
    *,
    btc_4h: Mapping[str, Any] | None = None,
    btc_1d: Mapping[str, Any] | None = None,
    symbol_1h: Mapping[str, Any] | None = None,
    entry_15m: Mapping[str, Any] | None = None,
    side_hint: str | None = None,
) -> DirectionDecision:
    """Decide whether long/short is allowed.

    Parameters are metric dictionaries from existing code. Missing data should
    not crash the bot; it should default to neutral and lower size.
    """
    btc_4h = btc_4h or {}
    btc_1d = btc_1d or {}
    symbol_1h = symbol_1h or {}
    entry_15m = entry_15m or {}

    btc_4h_bias = _close_ema_bias(btc_4h)
    btc_1d_bias = _close_ema_bias(btc_1d)
    symbol_bias = _close_ema_bias(symbol_1h)

    # Broad market regime.
    if btc_4h_bias == "bullish" and btc_1d_bias != "bearish":
        regime = "bullish"
    elif btc_4h_bias == "bearish" and btc_1d_bias != "bullish":
        regime = "bearish"
    else:
        regime = "neutral"

    long_allowed = regime in {"bullish", "neutral"} and symbol_bias in {"bullish", "neutral"}
    short_allowed = regime == "bearish" and symbol_bias in {"bearish", "neutral"}

    # Never let a bullish BTC regime freely short. This is a major simplification
    # to prevent countertrend overfitting.
    if regime == "bullish":
        short_allowed = False

    # If side_hint is known, keep the opposite side disabled only at the final stage.
    hint = str(side_hint or "").lower()
    if hint == "long":
        short_allowed = False
    elif hint == "short":
        long_allowed = False

    vm, v_reason = _volume_multiplier(entry_15m)
    qm, q_reason = _quality_multiplier(entry_15m)
    size_multiplier = round(max(0.0, min(1.0, vm * qm)), 2)

    if size_multiplier <= 0:
        long_allowed = False
        short_allowed = False

    reason = (
        f"regime={regime}, btc4h={btc_4h_bias}, btc1d={btc_1d_bias}, "
        f"symbol1h={symbol_bias}, {v_reason}, {q_reason}, "
        f"size_mult={size_multiplier:.2f}"
    )

    return DirectionDecision(
        long_allowed=long_allowed,
        short_allowed=short_allowed,
        regime=regime,
        reason=reason,
        size_multiplier=size_multiplier,
    )

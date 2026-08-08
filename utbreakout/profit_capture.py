"""Shared profit-capture helpers for the existing five-strategy suite.

The functions in this module are deliberately small and deterministic.  They
keep risk-quality gates from being compounded as if they were independent
probabilities, and keep holding periods expressed in the strategy's signal
timeframe when exits are monitored on a faster timeframe.
"""

from __future__ import annotations

from math import ceil, isfinite
from typing import Any, Iterable


EXISTING_ALPHA_PROFIT_PROFILE_VERSION = "existing_alpha_profit_capture_v1"

QUAD_CONFIRMATION_RISK_MULTIPLIERS = {
    1: 0.65,
    2: 0.85,
    3: 0.95,
    4: 1.00,
    5: 1.00,
}


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def weakest_link_risk_multiplier(
    values: Iterable[Any],
    *,
    cap: float = 1.0,
) -> float:
    """Return the strictest accepted quality budget without multiplying gates.

    Every input is already a final sizing opinion in the ``0..1`` range.  A
    minimum therefore preserves the most conservative opinion.  Multiplication
    would count the same volatility/liquidity weakness repeatedly and can turn
    several moderate reductions into a near-zero position.
    """

    ceiling = max(0.0, min(1.0, float(_finite(cap, 1.0) or 0.0)))
    normalized = [ceiling]
    for value in values:
        parsed = _finite(value)
        if parsed is not None:
            normalized.append(max(0.0, min(1.0, parsed)))
    return min(normalized)


def timeframe_minutes(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    unit = text[-1]
    try:
        amount = int(text[:-1])
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    multipliers = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    multiplier = multipliers.get(unit)
    return amount * multiplier if multiplier else None


def exit_bars_for_signal_holding_period(
    signal_bars: Any,
    *,
    signal_timeframe: Any,
    exit_timeframe: Any,
) -> int:
    """Convert a signal-bar holding period into exit-monitor bars."""

    bars = max(1, int(_finite(signal_bars, 1.0) or 1.0))
    signal_minutes = timeframe_minutes(signal_timeframe)
    exit_minutes = timeframe_minutes(exit_timeframe)
    if signal_minutes is None or exit_minutes is None:
        return bars
    return max(1, int(ceil(bars * signal_minutes / exit_minutes)))


def bounded_structure_anchor(
    *,
    entry_price: Any,
    atr_value: Any,
    structure_stop: Any,
    max_distance_atr: Any,
) -> tuple[float | None, float | None]:
    """Use a structure anchor only when it is close enough to define risk."""

    entry = _finite(entry_price)
    atr = _finite(atr_value)
    structure = _finite(structure_stop)
    maximum = _finite(max_distance_atr)
    if entry is None or atr is None or atr <= 0 or structure is None:
        return None, None
    distance_atr = abs(entry - structure) / atr
    if maximum is not None and maximum > 0 and distance_atr > maximum:
        return None, distance_atr
    return structure, distance_atr


__all__ = (
    "EXISTING_ALPHA_PROFIT_PROFILE_VERSION",
    "QUAD_CONFIRMATION_RISK_MULTIPLIERS",
    "bounded_structure_anchor",
    "exit_bars_for_signal_holding_period",
    "timeframe_minutes",
    "weakest_link_risk_multiplier",
)

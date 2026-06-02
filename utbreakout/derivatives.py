"""Public derivatives-context filters for UT Breakout alpha engines."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class DerivativesDecision:
    veto: bool = False
    reason: str = "DERIVATIVES_OK"
    size_multiplier: float = 1.0


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _field(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def missing_derivatives_data(context):
    return any(
        _field(context, key) is None
        for key in ("funding_rate", "oi_change_pct", "long_short_ratio")
    )


def derivatives_overheated_for_long(context, config=None):
    config = config or {}
    funding = _finite_float(_field(context, "funding_rate"), 0.0)
    long_short = _finite_float(_field(context, "long_short_ratio"), 1.0)
    oi_change = _finite_float(_field(context, "oi_change_pct"), 0.0)
    return (
        funding > _finite_float(config.get("funding_long_overheat"), 0.0010)
        and long_short > _finite_float(config.get("long_short_long_overheat"), 2.0)
        and oi_change > _finite_float(config.get("oi_overheat_pct"), 8.0)
    )


def derivatives_overheated_for_short(context, config=None):
    config = config or {}
    funding = _finite_float(_field(context, "funding_rate"), 0.0)
    long_short = _finite_float(_field(context, "long_short_ratio"), 1.0)
    oi_change = _finite_float(_field(context, "oi_change_pct"), 0.0)
    return (
        funding < _finite_float(config.get("funding_short_overheat"), -0.0010)
        and long_short < _finite_float(config.get("long_short_short_overheat"), 0.50)
        and oi_change > _finite_float(config.get("oi_overheat_pct"), 8.0)
    )


def evaluate_derivatives_filter(side, context=None, config=None):
    config = config or {}
    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"}:
        return DerivativesDecision(True, "DERIVATIVES_UNKNOWN_SIDE", 0.0)

    if missing_derivatives_data(context):
        if bool(config.get("derivatives_data_required", False)):
            return DerivativesDecision(True, "MISSING_DERIVATIVES_DATA", 0.0)
        return DerivativesDecision(False, "DERIVATIVES_DATA_PARTIAL", 0.85)

    if side == "LONG" and derivatives_overheated_for_long(context, config):
        return DerivativesDecision(True, "DERIVATIVES_LONG_OVERHEATED", 0.0)
    if side == "SHORT" and derivatives_overheated_for_short(context, config):
        return DerivativesDecision(True, "DERIVATIVES_SHORT_OVERHEATED", 0.0)

    funding = _finite_float(_field(context, "funding_rate"), 0.0)
    if side == "LONG" and funding > _finite_float(config.get("funding_long_reduce"), 0.0006):
        return DerivativesDecision(False, "DERIVATIVES_LONG_CROWDING_REDUCED", 0.7)
    if side == "SHORT" and funding < _finite_float(config.get("funding_short_reduce"), -0.0006):
        return DerivativesDecision(False, "DERIVATIVES_SHORT_CROWDING_REDUCED", 0.7)
    return DerivativesDecision()

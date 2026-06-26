"""Market-regime scoring helpers for UT Breakout.

These helpers are deliberately pure. They do not place orders and do not mutate
runtime bot state.
"""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class RegimeAction:
    allow_long: bool
    allow_short: bool
    risk_multiplier: float = 1.0
    preferred_exit: str | None = "HYBRID_DEFENSIVE"


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clamp(value, low, high):
    return max(float(low), min(float(high), float(value)))


def _field(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _score_above(value, low, high):
    return _clamp((_finite_float(value) - low) / max(high - low, 1e-9), 0.0, 1.0)


def _score_below(value, low, high):
    return _clamp((high - _finite_float(value)) / max(high - low, 1e-9), 0.0, 1.0)


def default_regime_config():
    return {
        "adx_trend_threshold": 22.0,
        "chop_choppy_threshold": 58.0,
        "chop_trend_threshold": 45.0,
        "atr_high_percent": 1.5,
        "atr_extreme_percent": 2.5,
        "atr_low_percent": 0.18,
        "width_compression_percent": 1.0,
        "range_expansion_high": 1.8,
        "volume_expansion_high": 1.5,
        "trend_risk_multiplier": 1.0,
        "choppy_risk_multiplier": 0.45,
        "high_vol_chaos_risk_multiplier": 0.35,
        "low_vol_compression_risk_multiplier": 0.55,
        "mixed_risk_multiplier": 0.75,
    }


def classify_regime(context=None, config=None):
    """Classify a final-entry regime using the brief's explicit labels."""
    config = config or {}
    spread_bps = _finite_float(_field(context, "spread_bps"), 0.0)
    if spread_bps > _finite_float(config.get("max_spread_bps"), 10.0):
        return "BAD_LIQUIDITY"

    abnormal = _finite_float(_field(context, "abnormal_candle_zscore"), 0.0)
    if abnormal >= _finite_float(config.get("abnormal_candle_zscore"), 4.0):
        return "NEWS_SPIKE_OR_ABNORMAL"

    crowding = _finite_float(_field(context, "derivatives_crowding_score"), 0.0)
    if crowding >= _finite_float(config.get("crowding_overheated_score"), 3.0):
        return "CROWDING_OVERHEATED"

    adx = _finite_float(_field(context, "adx"), 0.0)
    plus_di = _finite_float(_field(context, "plus_di"), 0.0)
    minus_di = _finite_float(_field(context, "minus_di"), 0.0)
    htf_trend = str(_field(context, "htf_trend", "") or "").upper()
    if adx >= _finite_float(config.get("trend_adx_min"), 25.0) and htf_trend == "UP" and plus_di > minus_di:
        return "TREND_UP"
    if adx >= _finite_float(config.get("trend_adx_min"), 25.0) and htf_trend == "DOWN" and minus_di > plus_di:
        return "TREND_DOWN"

    squeeze = _finite_float(_field(context, "squeeze_percentile"), None)
    atr_pctile = _finite_float(_field(context, "atr_percentile"), None)
    if squeeze is not None and atr_pctile is not None and squeeze <= 20 and atr_pctile <= 30:
        return "LOW_VOL_COMPRESSION"
    if atr_pctile is not None and atr_pctile >= 85:
        return "HIGH_VOL_EXPANSION"
    return "CHOP"


def regime_action(regime, signal=None, config=None):
    config = config or {}
    regime = str(regime or "").upper()
    has_breakout = bool(_field(signal, "has_breakout", True))
    volume_ratio = _finite_float(_field(signal, "volume_ratio"), 1.0)
    volume_min = _finite_float(config.get("regime_breakout_volume_ratio_min"), 1.20)

    if regime == "TREND_UP":
        return RegimeAction(True, False, 1.0, "HYBRID_DEFENSIVE")
    if regime == "TREND_DOWN":
        return RegimeAction(False, True, _finite_float(config.get("short_risk_multiplier"), 0.5), "HYBRID_DEFENSIVE")
    if regime == "LOW_VOL_COMPRESSION":
        allowed = has_breakout and volume_ratio >= volume_min
        return RegimeAction(allowed, allowed, 0.7, "VOL_ADAPTIVE_TP")
    if regime == "HIGH_VOL_EXPANSION":
        return RegimeAction(True, True, 0.5, "FIXED_TP_TIME_STOP")
    if regime in {"CROWDING_OVERHEATED", "CHOP", "NEWS_SPIKE_OR_ABNORMAL", "BAD_LIQUIDITY"}:
        return RegimeAction(False, False, 0.0, None)
    return RegimeAction(False, False, 0.0, None)


def classify_market_regime(values, cfg=None, side=None):
    cfg = {**default_regime_config(), **(cfg or {})}
    values = dict(values or {})
    side = str(side or values.get("side") or "").lower()

    adx = _finite_float(values.get("adx"))
    chop = _finite_float(values.get("chop"), 50.0)
    atr_pct = _finite_float(values.get("atr_pct"))
    hurst = _finite_float(values.get("hurst_exponent"), 0.5)
    bb_width = _finite_float(values.get("bb_width_pct"))
    keltner_width = _finite_float(values.get("keltner_width_pct"))
    range_expansion = _finite_float(values.get("range_expansion_ratio"), 1.0)
    volume_ratio = _finite_float(values.get("volume_ratio"), 1.0)
    slope = _finite_float(values.get("trend_slope_pct"))
    btc_direction = str(values.get("btc_direction") or values.get("btc_regime") or "").lower()
    momentum = _finite_float(values.get("momentum_12_pct"), _finite_float(values.get("momentum_6_pct")))

    trend_strength = (
        _score_above(adx, cfg["adx_trend_threshold"] * 0.7, cfg["adx_trend_threshold"] * 1.5) * 0.35
        + _score_below(chop, cfg["chop_trend_threshold"] * 0.7, cfg["chop_trend_threshold"] * 1.2) * 0.25
        + _score_above(hurst, 0.50, 0.62) * 0.15
        + _score_above(abs(slope), 0.02, 0.30) * 0.15
        + _score_above(abs(momentum), 0.20, 2.00) * 0.10
    )
    choppy_score = (
        _score_above(chop, 50.0, cfg["chop_choppy_threshold"] + 8.0) * 0.45
        + _score_below(adx, 10.0, cfg["adx_trend_threshold"]) * 0.30
        + _score_below(max(bb_width, keltner_width), 0.1, cfg["width_compression_percent"]) * 0.25
    )
    high_vol_score = (
        _score_above(atr_pct, cfg["atr_high_percent"], cfg["atr_extreme_percent"]) * 0.45
        + _score_above(range_expansion, 1.1, cfg["range_expansion_high"]) * 0.30
        + _score_above(volume_ratio, 1.0, cfg["volume_expansion_high"]) * 0.15
        + _score_above(chop, 50.0, 65.0) * 0.10
    )
    compression_score = (
        _score_below(atr_pct, cfg["atr_low_percent"] * 0.4, cfg["atr_low_percent"]) * 0.45
        + _score_below(max(bb_width, keltner_width), 0.1, cfg["width_compression_percent"]) * 0.35
        + _score_above(chop, 50.0, 65.0) * 0.20
    )

    direction_score = slope + momentum * 0.25
    if btc_direction in {"long", "bull", "bullish", "up"}:
        direction_score += 0.15
    elif btc_direction in {"short", "bear", "bearish", "down"}:
        direction_score -= 0.15
    trend_regime = "bull_trend" if direction_score >= 0 else "bear_trend"

    scores = {
        "trend": round(trend_strength * 100.0, 2),
        "choppy": round(choppy_score * 100.0, 2),
        "high_vol_chaos": round(high_vol_score * 100.0, 2),
        "low_vol_compression": round(compression_score * 100.0, 2),
    }
    if high_vol_score >= 0.65:
        regime = "high_vol_chaos"
        risk = cfg["high_vol_chaos_risk_multiplier"]
    elif compression_score >= 0.65:
        regime = "low_vol_compression"
        risk = cfg["low_vol_compression_risk_multiplier"]
    elif choppy_score >= 0.60 and trend_strength < 0.65:
        regime = "choppy"
        risk = cfg["choppy_risk_multiplier"]
    elif trend_strength >= 0.58:
        regime = trend_regime
        aligned = (side == "long" and regime == "bull_trend") or (side == "short" and regime == "bear_trend")
        risk = cfg["trend_risk_multiplier"] if aligned or side not in {"long", "short"} else 0.55
    else:
        regime = "mixed"
        risk = cfg["mixed_risk_multiplier"]

    score = {
        "bull_trend": scores["trend"] if direction_score >= 0 else max(0.0, scores["trend"] - 20.0),
        "bear_trend": scores["trend"] if direction_score < 0 else max(0.0, scores["trend"] - 20.0),
        "choppy": scores["choppy"],
        "high_vol_chaos": scores["high_vol_chaos"],
        "low_vol_compression": scores["low_vol_compression"],
    }.get(regime, max(scores.values()))

    return {
        "regime": regime,
        "regime_score": round(score, 2),
        "risk_multiplier": round(_clamp(risk, 0.0, 1.0), 4),
        "scores": scores,
        "summary": f"{regime} score={score:.1f} risk x{_clamp(risk, 0.0, 1.0):.2f}",
    }

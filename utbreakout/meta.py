"""Rule-based meta-labeling helpers for UT Breakout."""

from math import isfinite

from .adaptive import evaluate_shadow_triple_barrier


META_FEATURE_KEYS = [
    "atr_pct",
    "adx",
    "chop",
    "donchian_width_pct",
    "range_expansion_ratio",
    "volume_ratio",
    "funding_rate",
    "open_interest_usdt",
    "trend_health_score",
    "strategy_quality_score",
    "adaptive_timeframe_score",
    "momentum_6_pct",
    "momentum_12_pct",
    "momentum_24_pct",
    "trend_slope_pct",
    "hurst_exponent",
    "close_location",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "recent_rebound_pct",
    "recent_drawdown_pct",
    "hour_of_day",
    "coin_selector_score",
]


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clamp(value, low, high):
    return max(float(low), min(float(high), float(value)))


def build_meta_features(values=None, *, side=None, selected_timeframe=None):
    values = dict(values or {})
    features = {key: _finite_float(values.get(key), 0.0) for key in META_FEATURE_KEYS}
    raw_side = str(side or values.get("side") or "").lower()
    features["side"] = raw_side if raw_side in {"long", "short"} else "unknown"
    features["side_long"] = 1.0 if features["side"] == "long" else 0.0
    features["side_short"] = 1.0 if features["side"] == "short" else 0.0
    features["selected_timeframe"] = str(selected_timeframe or values.get("selected_timeframe") or values.get("entry_timeframe") or "")
    return features


def label_from_barrier_outcome(outcome):
    if not isinstance(outcome, dict):
        return None
    name = str(outcome.get("outcome") or "").lower()
    if name == "tp":
        label = 1
    elif name == "sl":
        label = 0
    elif name == "timeout":
        label = 1 if _finite_float(outcome.get("pnl_r"), 0.0) > 0 else 0
    else:
        return None
    result = dict(outcome)
    result["label"] = label
    result["runner_pnl_r"] = _finite_float(outcome.get("runner_pnl_r"), _finite_float(outcome.get("pnl_r"), 0.0))
    result["mfe_capture_ratio"] = _finite_float(outcome.get("mfe_capture_ratio"), 0.0)
    return result


def evaluate_meta_label(**kwargs):
    return label_from_barrier_outcome(evaluate_shadow_triple_barrier(**kwargs))


def estimate_meta_probability(features=None, stats=None, cfg=None):
    cfg = cfg or {}
    features = build_meta_features(features)
    stats = dict(stats or {})
    prob = _finite_float(cfg.get("base_probability"), 0.50)

    prob += (_finite_float(features.get("trend_health_score"), 50.0) - 50.0) / 250.0
    prob += (_finite_float(features.get("strategy_quality_score"), 50.0) - 50.0) / 260.0
    prob += (_finite_float(features.get("adaptive_timeframe_score"), 50.0) - 50.0) / 400.0
    prob += _clamp((_finite_float(features.get("adx")) - 18.0) / 80.0, -0.08, 0.10)
    prob -= _clamp((_finite_float(features.get("chop"), 50.0) - 55.0) / 120.0, 0.0, 0.10)
    prob += _clamp((_finite_float(features.get("range_expansion_ratio"), 1.0) - 1.0) / 12.0, -0.03, 0.06)
    prob += _clamp((_finite_float(features.get("volume_ratio"), 1.0) - 1.0) / 15.0, -0.03, 0.05)
    prob += _clamp(_finite_float(features.get("momentum_12_pct")) / 30.0, -0.05, 0.05)
    if features.get("side") == "short":
        prob -= _finite_float(cfg.get("short_squeeze_penalty"), 0.02)
    funding = _finite_float(features.get("funding_rate"))
    if features.get("side") == "long" and funding > 0:
        prob -= min(funding * 25.0, 0.04)
    elif features.get("side") == "short" and funding < 0:
        prob -= min(abs(funding) * 25.0, 0.04)

    sample_count = int(_finite_float(stats.get("sample_count"), 0))
    if sample_count >= int(cfg.get("min_samples", 8)):
        tp_rate = _finite_float(stats.get("tp_rate"), 0.5)
        avg_pnl = _finite_float(stats.get("avg_pnl_r"), 0.0)
        prob += (tp_rate - 0.5) * 0.20 + _clamp(avg_pnl, -1.0, 1.5) * 0.05

    return round(_clamp(prob, 0.05, 0.95), 4)


def decide_meta_trade(meta_probability, cfg=None):
    cfg = cfg or {}
    full = _finite_float(cfg.get("full_size_threshold"), 0.65)
    half = _finite_float(cfg.get("half_size_threshold"), 0.55)
    micro = _finite_float(cfg.get("micro_size_threshold"), 0.50)
    prob = _finite_float(meta_probability)
    if prob >= full:
        return {"action": "trade", "size_multiplier": 1.0, "bucket": "full", "meta_probability": prob}
    if prob >= half:
        return {"action": "trade", "size_multiplier": 0.5, "bucket": "reduced", "meta_probability": prob}
    if prob >= micro:
        return {"action": "micro_or_watch", "size_multiplier": 0.25, "bucket": "micro", "meta_probability": prob}
    return {"action": "block", "size_multiplier": 0.0, "bucket": "blocked", "meta_probability": prob}

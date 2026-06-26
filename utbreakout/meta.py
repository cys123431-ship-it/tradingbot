"""Rule-based meta-labeling helpers for UT Breakout."""

from dataclasses import dataclass
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


@dataclass(frozen=True)
class GateDecision:
    allow: bool
    probability: float | None = None
    reason: str = "META_GATE_DISABLED"
    size_multiplier: float = 1.0


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


def probability_to_size_multiplier(probability, config=None):
    prob = _finite_float(probability, 0.0)
    if prob >= _finite_float((config or {}).get("meta_gate_full_prob"), 0.70):
        return 1.20
    if prob >= _finite_float((config or {}).get("meta_gate_normal_prob"), 0.60):
        return 1.00
    if prob >= _finite_float((config or {}).get("meta_gate_reduced_prob"), 0.55):
        return 0.70
    return 0.0


def _bar_value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def triple_barrier_label(entry_index, data, side, config=None):
    """Return +1 profit-first, -1 stop-first, or 0 time/ambiguous.

    This is a compact labeler for research data. Conservative same-bar handling
    is used: if TP and SL are both reachable in one candle, the stop label wins.
    """
    config = config or {}
    rows = list(data or [])
    try:
        entry_index = int(entry_index)
    except (TypeError, ValueError):
        return 0
    if entry_index < 0 or entry_index >= len(rows):
        return 0
    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"}:
        side = str(side or "").lower()
        side = "LONG" if side == "long" else "SHORT" if side == "short" else side
    if side not in {"LONG", "SHORT"}:
        return 0

    entry_row = rows[entry_index]
    entry_price = _finite_float(_bar_value(entry_row, "close"))
    atr = _finite_float(_bar_value(entry_row, "atr"), _finite_float(config.get("atr"), 0.0))
    if entry_price is None or entry_price <= 0:
        return 0
    risk_distance = _finite_float(config.get("risk_distance"))
    if risk_distance is None or risk_distance <= 0:
        risk_distance = max(atr, 0.0) * _finite_float(config.get("label_stop_atr_multiplier"), 1.0)
    if risk_distance <= 0:
        risk_distance = entry_price * _finite_float(config.get("label_stop_pct"), 0.01)

    stop_r = _finite_float(config.get("label_stop_r"), 1.0)
    profit_r = _finite_float(config.get("label_profit_r"), 1.8)
    max_holding_bars = max(1, int(_finite_float(config.get("label_max_holding_bars"), 12) or 12))
    stop_distance = risk_distance * stop_r
    profit_distance = risk_distance * profit_r
    if side == "LONG":
        stop_price = entry_price - stop_distance
        profit_price = entry_price + profit_distance
    else:
        stop_price = entry_price + stop_distance
        profit_price = entry_price - profit_distance

    end = min(entry_index + max_holding_bars + 1, len(rows))
    for row in rows[entry_index + 1:end]:
        high = _finite_float(_bar_value(row, "high"))
        low = _finite_float(_bar_value(row, "low"))
        if high is None or low is None:
            continue
        if side == "LONG":
            if low <= stop_price:
                return -1
            if high >= profit_price:
                return 1
        else:
            if high >= stop_price:
                return -1
            if low <= profit_price:
                return 1
    return 0


def _model_probability(model, features):
    if model is None:
        return None
    if hasattr(model, "predict_proba"):
        values = [features]
        try:
            pred = model.predict_proba(values)
            row = pred[0]
            return _finite_float(row[1] if len(row) > 1 else row[0])
        except Exception:
            return None
    if callable(model):
        try:
            return _finite_float(model(features))
        except Exception:
            return None
    return None


def rule_based_meta_gate(signal=None, context=None, config=None):
    features = build_meta_features(context or {}, side=_field(signal, "side", _field(context, "side")))
    probability = estimate_meta_probability(features, cfg=config)
    threshold = _finite_float((config or {}).get("meta_gate_min_prob"), 0.55)
    if probability < threshold:
        return GateDecision(False, probability, "META_GATE_REJECT_LOW_PROB", 0.0)
    return GateDecision(True, probability, "META_GATE_APPROVE_RULES", probability_to_size_multiplier(probability, config))


def meta_label_gate(signal=None, context=None, model=None, config=None):
    config = config or {}
    if not bool(config.get("meta_label_gate_enabled", config.get("meta_labeling_enabled", False))):
        return GateDecision(True, None, "META_GATE_DISABLED", 1.0)

    features = build_meta_features(context or {}, side=_field(signal, "side", _field(context, "side")))
    probability = _model_probability(model, features)
    if probability is None:
        return rule_based_meta_gate(signal, context, config)

    threshold = _finite_float(config.get("meta_gate_min_prob"), 0.55)
    if probability < threshold:
        return GateDecision(False, probability, "META_GATE_REJECT_LOW_PROB", 0.0)
    return GateDecision(True, probability, "META_GATE_APPROVE", probability_to_size_multiplier(probability, config))


def apply_meta_gate_to_risk(risk_pct, gate, config=None):
    gate = gate if isinstance(gate, GateDecision) else GateDecision(True)
    risk = max(0.0, _finite_float(risk_pct, 0.0))
    risk *= _finite_float(gate.size_multiplier, 1.0)
    return min(risk, _finite_float((config or {}).get("max_risk_per_trade_pct"), 1.0))

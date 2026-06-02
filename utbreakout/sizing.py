"""Position-sizing overlays for UT Breakout."""

from math import isfinite


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


def _nested(obj, outer, inner, default=None):
    parent = _field(obj, outer, None)
    return _field(parent, inner, default)


def default_sizing_config():
    return {
        "base_risk_percent": 0.5,
        "volatility_target_atr_pct": 1.0,
        "volatility_target_min_multiplier": 0.25,
        "meta_min_multiplier": 0.25,
        "drawdown_soft_pct": 5.0,
        "drawdown_hard_pct": 15.0,
        "consecutive_loss_soft": 2,
        "consecutive_loss_hard": 5,
        "min_risk_multiplier": 0.0,
        "max_risk_multiplier": 1.0,
    }


def build_position_risk_multiplier(inputs=None, cfg=None):
    cfg = {**default_sizing_config(), **(cfg or {})}
    data = dict(inputs or {})
    reasons = []
    blocked = False

    atr_pct = _finite_float(data.get("atr_pct"), cfg["volatility_target_atr_pct"])
    target_atr = max(0.01, _finite_float(cfg.get("volatility_target_atr_pct"), 1.0))
    vol_multiplier = _clamp(target_atr / max(atr_pct, target_atr), cfg["volatility_target_min_multiplier"], 1.0)
    if vol_multiplier < 0.999:
        reasons.append(f"volatility x{vol_multiplier:.2f}")

    meta_probability = _finite_float(data.get("meta_probability"), 0.65)
    if meta_probability >= 0.65:
        meta_multiplier = 1.0
    elif meta_probability >= 0.55:
        meta_multiplier = 0.5
    elif meta_probability >= 0.50:
        meta_multiplier = cfg["meta_min_multiplier"]
    else:
        meta_multiplier = 0.0
        blocked = True
        reasons.append("meta block")

    trend_multiplier = _clamp(_finite_float(data.get("trend_health_multiplier"), 1.0), 0.0, 1.0)
    quality_multiplier = _clamp(_finite_float(data.get("strategy_quality_multiplier"), 1.0), 0.0, 1.0)
    regime_multiplier = _clamp(_finite_float(data.get("regime_risk_multiplier"), 1.0), 0.0, 1.0)
    performance_multiplier = 1.0
    if _finite_float(data.get("recent_avg_pnl_r"), 0.0) < 0:
        performance_multiplier = 0.75
        reasons.append("recent performance")

    drawdown_pct = max(0.0, _finite_float(data.get("drawdown_pct"), 0.0))
    soft_dd = max(0.0, _finite_float(cfg.get("drawdown_soft_pct"), 5.0))
    hard_dd = max(soft_dd + 0.01, _finite_float(cfg.get("drawdown_hard_pct"), 15.0))
    if drawdown_pct >= hard_dd:
        drawdown_multiplier = 0.0
        blocked = True
        reasons.append("hard drawdown")
    elif drawdown_pct > soft_dd:
        drawdown_multiplier = _clamp(1.0 - (drawdown_pct - soft_dd) / max(hard_dd - soft_dd, 1e-9), 0.25, 1.0)
        reasons.append(f"drawdown x{drawdown_multiplier:.2f}")
    else:
        drawdown_multiplier = 1.0

    losses = int(_finite_float(data.get("consecutive_losses"), 0))
    soft_loss = int(cfg.get("consecutive_loss_soft", 2) or 2)
    hard_loss = int(cfg.get("consecutive_loss_hard", 5) or 5)
    if losses >= hard_loss:
        loss_multiplier = 0.0
        blocked = True
        reasons.append("consecutive loss block")
    elif losses >= soft_loss:
        loss_multiplier = max(0.25, 1.0 - (losses - soft_loss + 1) * 0.20)
        reasons.append(f"loss streak x{loss_multiplier:.2f}")
    else:
        loss_multiplier = 1.0

    exposure_multiplier = _clamp(_finite_float(data.get("portfolio_exposure_multiplier"), 1.0), 0.0, 1.0)
    if bool(data.get("daily_loss_limit_hit")):
        blocked = True
        reasons.append("daily loss limit")

    multiplier = (
        vol_multiplier
        * meta_multiplier
        * trend_multiplier
        * quality_multiplier
        * regime_multiplier
        * performance_multiplier
        * drawdown_multiplier
        * loss_multiplier
        * exposure_multiplier
    )
    if blocked:
        multiplier = 0.0

    multiplier = _clamp(multiplier, cfg["min_risk_multiplier"], cfg["max_risk_multiplier"])
    return {
        "risk_multiplier": round(multiplier, 4),
        "blocked": bool(blocked or multiplier <= 0.0),
        "components": {
            "volatility": round(vol_multiplier, 4),
            "meta": round(meta_multiplier, 4),
            "trend_health": round(trend_multiplier, 4),
            "strategy_quality": round(quality_multiplier, 4),
            "market_regime": round(regime_multiplier, 4),
            "recent_performance": round(performance_multiplier, 4),
            "drawdown": round(drawdown_multiplier, 4),
            "consecutive_loss": round(loss_multiplier, 4),
            "portfolio_exposure": round(exposure_multiplier, 4),
        },
        "reasons": reasons,
    }


def calculate_adaptive_risk_pct(signal=None, context=None, regime_action=None, derivatives=None, gate=None, config=None):
    """Return the final percent risk after all defensive brakes.

    Percent units are used throughout: 0.5 means 0.5% account risk.
    """
    config = dict(config or {})
    risk = _finite_float(
        config.get("risk_per_trade_pct", config.get("risk_per_trade_percent", config.get("base_risk_pct", 0.5))),
        0.5,
    )
    risk *= _finite_float(_field(regime_action, "risk_multiplier"), 1.0)
    risk *= _finite_float(_field(derivatives, "size_multiplier"), 1.0)
    risk *= _finite_float(_field(gate, "size_multiplier"), 1.0)

    atr_percentile = _finite_float(_field(context, "atr_percentile"), None)
    if atr_percentile is not None:
        if atr_percentile >= _finite_float(config.get("high_vol_risk_reduce_percentile"), 85.0):
            risk *= _finite_float(config.get("high_vol_risk_multiplier"), 0.5)
        if atr_percentile <= _finite_float(config.get("low_vol_risk_reduce_percentile"), 10.0):
            risk *= _finite_float(config.get("low_vol_risk_multiplier"), 0.5)

    losses = int(_finite_float(_nested(context, "account", "consecutive_losses", _field(context, "consecutive_losses", 0)), 0) or 0)
    if losses >= int(config.get("hard_stop_consecutive_losses", 3) or 3):
        return 0.0
    if losses >= int(config.get("soft_reduce_consecutive_losses", 2) or 2):
        risk *= _finite_float(config.get("loss_streak_risk_multiplier"), 0.5)

    daily_loss_r = _finite_float(_nested(context, "account", "daily_loss_r", _field(context, "daily_loss_r", 0.0)), 0.0)
    weekly_loss_r = _finite_float(_nested(context, "account", "weekly_loss_r", _field(context, "weekly_loss_r", 0.0)), 0.0)
    if daily_loss_r <= -_finite_float(config.get("daily_loss_limit_r"), 2.0):
        return 0.0
    if weekly_loss_r <= -_finite_float(config.get("weekly_loss_limit_r"), 5.0):
        return 0.0

    total_open_risk = _finite_float(
        _nested(context, "portfolio", "total_open_risk_pct", _field(context, "total_open_risk_pct", 0.0)),
        0.0,
    )
    same_direction = int(
        _finite_float(
            _nested(context, "portfolio", "same_direction_positions", _field(context, "same_direction_positions", 0)),
            0,
        ) or 0
    )
    if total_open_risk >= _finite_float(config.get("max_total_open_risk_pct"), 2.0):
        return 0.0
    if same_direction >= int(config.get("max_same_direction_positions", 2) or 2):
        return 0.0

    if risk <= 0:
        return 0.0
    min_risk = _finite_float(config.get("min_risk_per_trade_pct", config.get("min_risk_per_trade_percent")), 0.05)
    max_risk = _finite_float(config.get("max_risk_per_trade_pct", config.get("max_risk_per_trade_percent")), 1.0)
    return round(_clamp(risk, min_risk, max_risk), 8)

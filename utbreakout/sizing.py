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


def default_aggressive_growth_config():
    return {
        "aggressive_growth_enabled": False,
        "aggressive_growth_balance_sleeve_pct": 0.20,
        "aggressive_growth_max_trade_risk_pct": 0.015,
        "aggressive_growth_max_trade_risk_pct_strong": 0.025,
        "aggressive_growth_daily_loss_limit_pct": 0.04,
        "aggressive_growth_weekly_loss_limit_pct": 0.10,
        "aggressive_growth_max_open_positions": 2,
        "aggressive_growth_max_symbol_exposure_pct": 0.10,
        "aggressive_growth_pyramiding_enabled": True,
        "aggressive_growth_pyramid_trigger_r": 1.0,
        "aggressive_growth_pyramid_max_adds": 2,
        "aggressive_growth_pyramid_add_risk_fraction": 0.50,
        "aggressive_growth_move_sl_to_breakeven_before_add": True,
        "aggressive_growth_trailing_atr_multiplier": 2.5,
        "aggressive_growth_runner_pct": 0.35,
        "aggressive_growth_tp1_pct": 0.35,
        "aggressive_growth_tp2_pct": 0.30,
    }


def normalize_aggressive_growth_config(config=None):
    cfg = {**default_aggressive_growth_config(), **(config or {})}
    cfg["aggressive_growth_enabled"] = bool(cfg.get("aggressive_growth_enabled", False))
    cfg["aggressive_growth_pyramiding_enabled"] = bool(cfg.get("aggressive_growth_pyramiding_enabled", True))
    cfg["aggressive_growth_move_sl_to_breakeven_before_add"] = bool(
        cfg.get("aggressive_growth_move_sl_to_breakeven_before_add", True)
    )
    for key in (
        "aggressive_growth_balance_sleeve_pct",
        "aggressive_growth_max_trade_risk_pct",
        "aggressive_growth_max_trade_risk_pct_strong",
        "aggressive_growth_daily_loss_limit_pct",
        "aggressive_growth_weekly_loss_limit_pct",
        "aggressive_growth_max_symbol_exposure_pct",
        "aggressive_growth_pyramid_trigger_r",
        "aggressive_growth_pyramid_add_risk_fraction",
        "aggressive_growth_trailing_atr_multiplier",
        "aggressive_growth_runner_pct",
        "aggressive_growth_tp1_pct",
        "aggressive_growth_tp2_pct",
    ):
        cfg[key] = max(0.0, _finite_float(cfg.get(key), default_aggressive_growth_config()[key]))
    cfg["aggressive_growth_balance_sleeve_pct"] = _clamp(cfg["aggressive_growth_balance_sleeve_pct"], 0.0, 1.0)
    cfg["aggressive_growth_max_symbol_exposure_pct"] = _clamp(
        cfg["aggressive_growth_max_symbol_exposure_pct"], 0.0, 1.0
    )
    cfg["aggressive_growth_pyramid_add_risk_fraction"] = _clamp(
        cfg["aggressive_growth_pyramid_add_risk_fraction"], 0.0, 1.0
    )
    for key in ("aggressive_growth_runner_pct", "aggressive_growth_tp1_pct", "aggressive_growth_tp2_pct"):
        cfg[key] = _clamp(cfg[key], 0.0, 1.0)
    cfg["aggressive_growth_max_open_positions"] = max(
        1, int(_finite_float(cfg.get("aggressive_growth_max_open_positions"), 2))
    )
    cfg["aggressive_growth_pyramid_max_adds"] = max(
        0, int(_finite_float(cfg.get("aggressive_growth_pyramid_max_adds"), 2))
    )
    return cfg


def _positive_float_from(*values):
    for value in values:
        parsed = _finite_float(value, 0.0)
        if parsed > 0:
            return parsed
    return 0.0


def _growth_score(context):
    data = dict(context or {})
    explicit = _finite_float(data.get("growth_score"), None)
    if explicit is not None:
        return _clamp(explicit, 0.0, 100.0)
    score = 0.0
    score += 20.0 if bool(data.get("htf_trend_bullish")) else 0.0
    score += 20.0 if bool(data.get("symbol_trend_bullish")) else 0.0
    score += 15.0 if bool(data.get("volume_ok")) else 0.0
    score += 15.0 if bool(data.get("adx_ok") or data.get("trend_strength_ok")) else 0.0
    score += 15.0 if bool(data.get("quality_ok")) else 0.0
    score += 10.0 if bool(data.get("funding_not_overheated", True)) else 0.0
    score += 5.0 if bool(data.get("volatility_safe", True)) else 0.0
    return _clamp(score, 0.0, 100.0)


def _growth_score_to_risk_pct(score, cfg):
    if score >= 80.0:
        return cfg["aggressive_growth_max_trade_risk_pct_strong"]
    if score >= 65.0:
        return cfg["aggressive_growth_max_trade_risk_pct"]
    if score >= 50.0:
        return cfg["aggressive_growth_max_trade_risk_pct"] * 0.5
    return 0.0


def build_aggressive_growth_overlay_plan(base_plan=None, config=None, context=None):
    """Return an adjusted UTBreakout plan when the aggressive growth sleeve is enabled.

    Risk pct values are decimals: 0.015 means 1.5% account risk.
    """
    cfg = normalize_aggressive_growth_config(config)
    plan = dict(base_plan or {})
    data = dict(context or {})
    if not cfg["aggressive_growth_enabled"]:
        return {"enabled": False, "accepted": False, "reason": "disabled", "plan": plan}

    side = str(data.get("side") or plan.get("side") or "").lower()
    entry = _positive_float_from(data.get("entry_price"), plan.get("entry_price"))
    stop = _positive_float_from(data.get("stop_loss_price"), data.get("stop_loss"), plan.get("stop_loss"))
    risk_distance = _positive_float_from(data.get("risk_distance"), plan.get("risk_distance"))
    if entry > 0 and stop > 0:
        risk_distance = abs(entry - stop)
    equity = _positive_float_from(data.get("account_equity"), data.get("equity"), data.get("total_balance_usdt"))

    hard_reasons = []
    if side != "long":
        hard_reasons.append("aggressive growth is long-only")
    if entry <= 0:
        hard_reasons.append("entry price unavailable")
    if risk_distance <= 0:
        hard_reasons.append("SL distance unavailable")
    if equity <= 0:
        hard_reasons.append("futures balance unavailable")
    if data.get("liquidity_ok") is False or data.get("spread_ok") is False:
        hard_reasons.append("liquidity/spread abnormal")

    daily_pnl = _finite_float(data.get("daily_pnl_usdt"), 0.0)
    weekly_pnl = _finite_float(data.get("weekly_pnl_usdt"), 0.0)
    daily_limit = equity * cfg["aggressive_growth_daily_loss_limit_pct"]
    weekly_limit = equity * cfg["aggressive_growth_weekly_loss_limit_pct"]
    if equity > 0 and daily_limit > 0 and daily_pnl <= -daily_limit:
        hard_reasons.append("daily loss limit reached")
    if equity > 0 and weekly_limit > 0 and weekly_pnl <= -weekly_limit:
        hard_reasons.append("weekly loss limit reached")
    open_positions = int(_finite_float(data.get("open_positions"), 0))
    if open_positions >= cfg["aggressive_growth_max_open_positions"]:
        hard_reasons.append("aggressive open position limit reached")

    sleeve_cap = equity * cfg["aggressive_growth_balance_sleeve_pct"]
    current_sleeve = max(0.0, _finite_float(data.get("total_aggressive_exposure_notional"), 0.0))
    available_sleeve = max(0.0, sleeve_cap - current_sleeve)
    symbol_cap = equity * cfg["aggressive_growth_max_symbol_exposure_pct"]
    current_symbol = max(0.0, _finite_float(data.get("symbol_exposure_notional"), 0.0))
    available_symbol = max(0.0, symbol_cap - current_symbol) if symbol_cap > 0 else available_sleeve
    if sleeve_cap <= 0 or available_sleeve <= 0:
        hard_reasons.append("aggressive sleeve exhausted")
    if symbol_cap > 0 and available_symbol <= 0:
        hard_reasons.append("symbol exposure cap reached")
    if hard_reasons:
        return {
            "enabled": True,
            "accepted": False,
            "reason": "; ".join(hard_reasons),
            "hard_reasons": hard_reasons,
            "plan": plan,
        }

    score = _growth_score(data)
    risk_pct = _growth_score_to_risk_pct(score, cfg)
    if risk_pct <= 0:
        return {
            "enabled": True,
            "accepted": False,
            "reason": f"growth score too low: {score:.1f}",
            "growth_score": round(score, 2),
            "plan": plan,
        }

    risk_amount = equity * risk_pct
    qty_by_risk = risk_amount / max(risk_distance, 1e-12)
    qty_by_sleeve = available_sleeve / max(entry, 1e-12)
    qty_by_symbol = available_symbol / max(entry, 1e-12) if available_symbol > 0 else qty_by_sleeve
    exchange_max_qty = _positive_float_from(data.get("exchange_max_qty"), data.get("max_qty"))
    qty_candidates = [qty_by_risk, qty_by_sleeve, qty_by_symbol]
    if exchange_max_qty > 0:
        qty_candidates.append(exchange_max_qty)
    qty = min(qty_candidates)
    if qty <= 0:
        return {
            "enabled": True,
            "accepted": False,
            "reason": "calculated aggressive quantity is zero",
            "growth_score": round(score, 2),
            "plan": plan,
        }

    leverage = max(1.0, _finite_float(data.get("leverage", plan.get("leverage")), 1.0))
    planned_notional = qty * entry
    actual_risk = qty * risk_distance
    adjusted = dict(plan)
    adjusted.update({
        "qty": qty,
        "risk_usdt": actual_risk,
        "planned_notional": planned_notional,
        "planned_margin": planned_notional / leverage,
        "risk_per_trade_percent": risk_pct * 100.0,
        "aggressive_growth_overlay": True,
        "aggressive_growth_score": round(score, 2),
        "aggressive_growth_risk_pct": risk_pct,
        "aggressive_growth_sleeve_cap_usdt": sleeve_cap,
        "aggressive_growth_available_sleeve_usdt": available_sleeve,
        "aggressive_growth_qty_by_risk": qty_by_risk,
        "aggressive_growth_qty_by_sleeve": qty_by_sleeve,
        "aggressive_growth_qty_by_symbol": qty_by_symbol,
        "partial_take_profit_enabled": True,
        "partial_take_profit_r_multiple": 1.0,
        "partial_take_profit_ratio": cfg["aggressive_growth_tp1_pct"],
        "second_take_profit_enabled": cfg["aggressive_growth_tp2_pct"] > 0,
        "second_take_profit_r_multiple": 2.0,
        "second_take_profit_ratio": cfg["aggressive_growth_tp2_pct"],
        "runner_pct": cfg["aggressive_growth_runner_pct"],
        "atr_trailing_enabled": True,
        "atr_trailing_multiplier": cfg["aggressive_growth_trailing_atr_multiplier"],
        "runner_exit_enabled": True,
        "runner_chandelier_enabled": True,
        "tp1_breakeven_enabled": True,
    })
    return {
        "enabled": True,
        "accepted": True,
        "reason": f"growth score {score:.1f}, risk {risk_pct * 100:.2f}%",
        "growth_score": round(score, 2),
        "risk_pct": risk_pct,
        "plan": adjusted,
    }


def build_aggressive_growth_pyramid_plan(position=None, config=None, context=None):
    cfg = normalize_aggressive_growth_config(config)
    pos = dict(position or {})
    data = dict(context or {})
    if not cfg["aggressive_growth_enabled"] or not cfg["aggressive_growth_pyramiding_enabled"]:
        return {"enabled": False, "accepted": False, "reason": "disabled"}

    side = str(data.get("side") or pos.get("side") or "").lower()
    entry = _positive_float_from(data.get("entry_price"), pos.get("entry_price"), pos.get("entryPrice"))
    current = _positive_float_from(data.get("current_price"), data.get("mark_price"))
    risk_distance = _positive_float_from(data.get("risk_distance"), pos.get("risk_distance"))
    initial_qty = _positive_float_from(data.get("initial_qty"), pos.get("initial_qty"), pos.get("qty"))
    add_count = int(_finite_float(data.get("pyramid_add_count", pos.get("pyramid_add_count")), 0))
    stop_price = _positive_float_from(data.get("stop_loss_price"), pos.get("stop_loss_price"), pos.get("last_stop_price"))
    equity = _positive_float_from(data.get("account_equity"), data.get("equity"), data.get("total_balance_usdt"))
    reasons = []
    if side != "long":
        reasons.append("pyramiding is long-only")
    if entry <= 0 or current <= 0 or risk_distance <= 0 or initial_qty <= 0:
        reasons.append("position metrics incomplete")
    if add_count >= cfg["aggressive_growth_pyramid_max_adds"]:
        reasons.append("pyramid add limit reached")
    if reasons:
        return {"enabled": True, "accepted": False, "reason": "; ".join(reasons), "reasons": reasons}

    pnl_r = (current - entry) / max(risk_distance, 1e-12)
    if pnl_r < cfg["aggressive_growth_pyramid_trigger_r"]:
        return {
            "enabled": True,
            "accepted": False,
            "reason": f"pnl {pnl_r:.2f}R below trigger",
            "pnl_r": round(pnl_r, 4),
        }
    if cfg["aggressive_growth_move_sl_to_breakeven_before_add"]:
        if data.get("sl_can_move_to_breakeven") is False:
            return {
                "enabled": True,
                "accepted": False,
                "reason": "SL cannot move to breakeven before add",
                "pnl_r": round(pnl_r, 4),
            }
        breakeven_stop = max(stop_price, entry)
    else:
        breakeven_stop = stop_price

    add_qty_cap = initial_qty * cfg["aggressive_growth_pyramid_add_risk_fraction"]
    sleeve_qty_cap = _positive_float_from(data.get("available_sleeve_qty"))
    if sleeve_qty_cap > 0:
        add_qty_cap = min(add_qty_cap, sleeve_qty_cap)
    max_loss = equity * cfg["aggressive_growth_max_trade_risk_pct"] if equity > 0 else float("inf")
    add_worst_loss = max(0.0, current - breakeven_stop) * add_qty_cap
    if add_worst_loss > max_loss:
        risk_limited_qty = max_loss / max(current - breakeven_stop, 1e-12)
        add_qty_cap = min(add_qty_cap, risk_limited_qty)
        add_worst_loss = max(0.0, current - breakeven_stop) * add_qty_cap
    if add_qty_cap <= 0:
        return {
            "enabled": True,
            "accepted": False,
            "reason": "pyramid quantity is zero after risk caps",
            "pnl_r": round(pnl_r, 4),
        }
    return {
        "enabled": True,
        "accepted": True,
        "reason": f"pyramid add allowed at {pnl_r:.2f}R",
        "pnl_r": round(pnl_r, 4),
        "add_qty": add_qty_cap,
        "breakeven_stop_price": breakeven_stop,
        "requires_sl_move": cfg["aggressive_growth_move_sl_to_breakeven_before_add"] and stop_price < entry,
        "worst_loss_usdt": add_worst_loss,
        "pyramid_add_count": add_count + 1,
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

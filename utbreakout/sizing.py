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
        "aggressive_growth_sleeve_mode": "notional",
        "aggressive_growth_max_leverage_for_margin_sleeve": 3.0,
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
        "aggressive_growth_vol_target_enabled": True,
        "aggressive_growth_atr_pct_low": 0.004,
        "aggressive_growth_atr_pct_normal": 0.012,
        "aggressive_growth_atr_pct_high": 0.025,
        "aggressive_growth_atr_pct_extreme": 0.040,
        "aggressive_growth_kelly_enabled": False,
        "aggressive_growth_kelly_lookback_trades": 50,
        "aggressive_growth_kelly_fraction": 0.25,
        "aggressive_growth_kelly_max_risk_multiplier": 1.25,
        "aggressive_growth_kelly_min_risk_multiplier": 0.25,
        "aggressive_growth_cppi_enabled": False,
        "aggressive_growth_cppi_floor_pct": 0.90,
        "aggressive_growth_cppi_multiplier": 2.0,
        "aggressive_growth_cppi_min_sleeve_pct": 0.05,
        "aggressive_growth_cppi_max_sleeve_pct": 0.20,
    }


def normalize_aggressive_growth_config(config=None):
    cfg = {**default_aggressive_growth_config(), **(config or {})}
    cfg["aggressive_growth_enabled"] = bool(cfg.get("aggressive_growth_enabled", False))
    cfg["aggressive_growth_pyramiding_enabled"] = bool(cfg.get("aggressive_growth_pyramiding_enabled", True))
    cfg["aggressive_growth_move_sl_to_breakeven_before_add"] = bool(
        cfg.get("aggressive_growth_move_sl_to_breakeven_before_add", True)
    )
    cfg["aggressive_growth_vol_target_enabled"] = bool(cfg.get("aggressive_growth_vol_target_enabled", True))
    cfg["aggressive_growth_kelly_enabled"] = bool(cfg.get("aggressive_growth_kelly_enabled", False))
    cfg["aggressive_growth_cppi_enabled"] = bool(cfg.get("aggressive_growth_cppi_enabled", False))
    sleeve_mode = str(cfg.get("aggressive_growth_sleeve_mode", "notional") or "notional").strip().lower()
    cfg["aggressive_growth_sleeve_mode"] = sleeve_mode if sleeve_mode in {"notional", "margin"} else "notional"
    for key in (
        "aggressive_growth_balance_sleeve_pct",
        "aggressive_growth_max_leverage_for_margin_sleeve",
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
        "aggressive_growth_atr_pct_low",
        "aggressive_growth_atr_pct_normal",
        "aggressive_growth_atr_pct_high",
        "aggressive_growth_atr_pct_extreme",
        "aggressive_growth_kelly_fraction",
        "aggressive_growth_kelly_max_risk_multiplier",
        "aggressive_growth_kelly_min_risk_multiplier",
        "aggressive_growth_cppi_floor_pct",
        "aggressive_growth_cppi_multiplier",
        "aggressive_growth_cppi_min_sleeve_pct",
        "aggressive_growth_cppi_max_sleeve_pct",
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
    cfg["aggressive_growth_kelly_lookback_trades"] = max(
        1, int(_finite_float(cfg.get("aggressive_growth_kelly_lookback_trades"), 50))
    )
    return cfg


def _positive_float_from(*values):
    for value in values:
        parsed = _finite_float(value, 0.0)
        if parsed > 0:
            return parsed
    return 0.0


def _optional_float(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def is_aggressive_symbol_trend_bullish(trend_health=None):
    state = _field(trend_health, "state", trend_health)
    if state is True:
        return True
    if isinstance(state, str):
        return state.strip().lower() in {"pass", "strong", "bullish", "uptrend", "trend", "true"}
    return False


def evaluate_derivatives_growth_score(context=None):
    data = dict(context or {})
    score = 0.0
    reasons = []

    funding = _optional_float(data.get("funding_rate"))
    funding_pct_7d = _optional_float(data.get("funding_percentile_7d"))
    funding_pct_30d = _optional_float(data.get("funding_percentile_30d"))
    oi_chg_1h = _optional_float(data.get("open_interest_change_1h", data.get("open_interest_delta_pct")))
    oi_chg_4h = _optional_float(data.get("open_interest_change_4h"))
    price_chg_1h = _optional_float(data.get("price_change_1h"))
    price_chg_4h = _optional_float(data.get("price_change_4h"))
    long_short = _optional_float(data.get("long_short_ratio"))
    taker_ratio = _optional_float(data.get("taker_buy_sell_ratio"))
    liquidation_imbalance = _optional_float(data.get("liquidation_imbalance"))
    if funding_pct_7d is not None and funding_pct_7d > 1.0:
        funding_pct_7d /= 100.0
    if funding_pct_30d is not None and funding_pct_30d > 1.0:
        funding_pct_30d /= 100.0

    if price_chg_1h is not None and oi_chg_1h is not None:
        if price_chg_1h > 0 and oi_chg_1h > 0:
            score += 15.0
            reasons.append("price_up_oi_up")
        elif price_chg_1h <= 0 and oi_chg_1h > 0:
            score -= 8.0
            reasons.append("oi_up_price_stall")
    if price_chg_4h is not None and oi_chg_4h is not None:
        if price_chg_4h > 0 and oi_chg_4h > 0:
            score += 8.0
            reasons.append("price_4h_up_oi_4h_up")
        elif price_chg_4h <= 0 and oi_chg_4h > 0:
            score -= 8.0
            reasons.append("oi_4h_up_price_stall")

    if funding is not None:
        if funding <= 0.0003:
            score += 10.0
            reasons.append("funding_calm")
        elif funding <= 0.0008:
            score += 3.0
            reasons.append("funding_moderate")
        else:
            score -= 15.0
            reasons.append("funding_overheated")

    for percentile, label in ((funding_pct_7d, "7d"), (funding_pct_30d, "30d")):
        if percentile is None:
            continue
        if percentile >= 0.90:
            score -= 15.0
            reasons.append(f"funding_percentile_{label}_extreme")
        elif percentile <= 0.60:
            score += 5.0
            reasons.append(f"funding_percentile_{label}_safe")

    if long_short is not None:
        if long_short >= 2.5:
            score -= 15.0
            reasons.append("long_crowded")
        elif 0.8 <= long_short <= 1.6:
            score += 5.0
            reasons.append("long_short_balanced")

    if taker_ratio is not None:
        if taker_ratio > 1.05:
            score += 5.0
            reasons.append("taker_buy_pressure")
        elif taker_ratio < 0.95:
            score -= 5.0
            reasons.append("taker_sell_pressure")

    if liquidation_imbalance is not None:
        if liquidation_imbalance > 0:
            score += 3.0
            reasons.append("short_liquidation_tailwind")
        elif liquidation_imbalance < -0.5:
            score -= 5.0
            reasons.append("long_liquidation_pressure")

    return round(score, 4), reasons


def choose_aggressive_exit_split(growth_score, cfg=None):
    score = _finite_float(growth_score, 0.0)
    if score >= 85.0:
        split = {"tp1_pct": 0.20, "tp2_pct": 0.20, "runner_pct": 0.60, "mode": "super_strong_trend"}
    elif score >= 75.0:
        split = {"tp1_pct": 0.25, "tp2_pct": 0.25, "runner_pct": 0.50, "mode": "strong_trend"}
    elif score >= 60.0:
        split = {"tp1_pct": 0.35, "tp2_pct": 0.30, "runner_pct": 0.35, "mode": "normal_growth"}
    else:
        split = {"tp1_pct": 0.40, "tp2_pct": 0.35, "runner_pct": 0.25, "mode": "weak_growth"}
    total = split["tp1_pct"] + split["tp2_pct"] + split["runner_pct"]
    if total <= 0:
        return {"tp1_pct": 0.35, "tp2_pct": 0.30, "runner_pct": 0.35, "mode": "normal_growth"}
    split["tp1_pct"] = round(split["tp1_pct"] / total, 10)
    split["tp2_pct"] = round(split["tp2_pct"] / total, 10)
    split["runner_pct"] = round(1.0 - split["tp1_pct"] - split["tp2_pct"], 10)
    return split


def apply_aggressive_volatility_targeting(risk_pct, atr_pct, cfg=None):
    cfg = normalize_aggressive_growth_config(cfg)
    risk = max(0.0, _finite_float(risk_pct, 0.0))
    if not cfg["aggressive_growth_vol_target_enabled"]:
        return risk, "vol_target_disabled"
    atr = _optional_float(atr_pct)
    if atr is not None and atr > 0.5:
        atr = atr / 100.0
    if atr is None:
        return risk * 0.8, "atr_missing"
    if atr >= cfg["aggressive_growth_atr_pct_extreme"]:
        return 0.0, "atr_extreme_block"
    if atr >= cfg["aggressive_growth_atr_pct_high"]:
        return risk * 0.5, "atr_high_reduce"
    if atr <= cfg["aggressive_growth_atr_pct_low"]:
        return risk * 0.8, "atr_too_low_reduce"
    return risk, "atr_normal"


def calculate_fractional_kelly_multiplier(trades=None, cfg=None):
    cfg = normalize_aggressive_growth_config(cfg)
    if not cfg["aggressive_growth_kelly_enabled"]:
        return 1.0, "kelly_disabled"
    lookback = cfg["aggressive_growth_kelly_lookback_trades"]
    recent = list(trades or [])[-lookback:]
    if len(recent) < 20:
        return 0.75, "not_enough_trades"

    values = []
    for trade in recent:
        value = _field(trade, "r_multiple", _field(trade, "pnl_r", trade))
        parsed = _optional_float(value)
        if parsed is not None:
            values.append(parsed)
    if len(values) < 20:
        return 0.75, "not_enough_trades"

    wins = [value for value in values if value > 0]
    losses = [abs(value) for value in values if value < 0]
    if not wins or not losses:
        return 0.75, "insufficient_win_loss_data"
    win_rate = len(wins) / len(values)
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    b = avg_win / max(avg_loss, 1e-12)
    p = win_rate
    q = 1.0 - p
    kelly = (b * p - q) / max(b, 1e-12)
    if kelly <= 0:
        return cfg["aggressive_growth_kelly_min_risk_multiplier"], "negative_kelly"
    fractional = kelly * cfg["aggressive_growth_kelly_fraction"]
    multiplier = _clamp(
        fractional,
        cfg["aggressive_growth_kelly_min_risk_multiplier"],
        cfg["aggressive_growth_kelly_max_risk_multiplier"],
    )
    return round(multiplier, 6), "kelly_applied"


def calculate_cppi_sleeve_pct(equity, high_watermark=None, cfg=None):
    cfg = normalize_aggressive_growth_config(cfg)
    base = cfg["aggressive_growth_balance_sleeve_pct"]
    if not cfg["aggressive_growth_cppi_enabled"]:
        return base
    eq = max(0.0, _finite_float(equity, 0.0))
    if eq <= 0:
        return cfg["aggressive_growth_cppi_min_sleeve_pct"]
    hwm = _finite_float(high_watermark, 0.0)
    if hwm <= 0:
        hwm = eq
    floor = hwm * cfg["aggressive_growth_cppi_floor_pct"]
    cushion = max(0.0, eq - floor)
    risky_budget = cushion * cfg["aggressive_growth_cppi_multiplier"]
    sleeve_pct = risky_budget / max(eq, 1e-12)
    return _clamp(
        sleeve_pct,
        cfg["aggressive_growth_cppi_min_sleeve_pct"],
        cfg["aggressive_growth_cppi_max_sleeve_pct"],
    )


def calculate_aggressive_sleeve_notional_cap(equity, leverage=None, cfg=None, high_watermark=None):
    cfg = normalize_aggressive_growth_config(cfg)
    sleeve_pct = calculate_cppi_sleeve_pct(equity, high_watermark, cfg)
    eq = max(0.0, _finite_float(equity, 0.0))
    if cfg["aggressive_growth_sleeve_mode"] == "margin":
        lev = max(1.0, _finite_float(leverage, 1.0))
        lev_cap = max(1.0, cfg["aggressive_growth_max_leverage_for_margin_sleeve"])
        return eq * sleeve_pct * min(lev, lev_cap), sleeve_pct
    return eq * sleeve_pct, sleeve_pct


def _growth_score(context):
    data = dict(context or {})
    explicit = _finite_float(data.get("growth_score"), None)
    if explicit is not None:
        score = _clamp(explicit, 0.0, 100.0)
        if data.get("symbol_trend_bullish") is False:
            score -= 20.0
    else:
        score = 0.0
        score += 20.0 if bool(data.get("htf_trend_bullish")) else 0.0
        if bool(data.get("symbol_trend_bullish")):
            score += 20.0
        else:
            score -= 20.0
        score += 15.0 if bool(data.get("volume_ok")) else 0.0
        score += 15.0 if bool(data.get("adx_ok") or data.get("trend_strength_ok")) else 0.0
        score += 15.0 if bool(data.get("quality_ok")) else 0.0
        if data.get("funding_not_overheated") is True:
            score += 5.0
        score += 5.0 if bool(data.get("volatility_safe", True)) else 0.0
    derivatives_score, _ = evaluate_derivatives_growth_score(data)
    score += derivatives_score
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

    leverage = max(1.0, _finite_float(data.get("leverage", plan.get("leverage")), 1.0))
    sleeve_cap, sleeve_pct = calculate_aggressive_sleeve_notional_cap(
        equity,
        leverage=leverage,
        cfg=cfg,
        high_watermark=data.get("aggressive_growth_high_watermark", data.get("high_watermark")),
    )
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

    derivatives_score, derivatives_reasons = evaluate_derivatives_growth_score(data)
    score = _growth_score(data)
    risk_pct = _growth_score_to_risk_pct(score, cfg)
    if risk_pct <= 0:
        return {
            "enabled": True,
            "accepted": False,
            "reason": f"growth score too low: {score:.1f}",
            "growth_score": round(score, 2),
            "derivatives_score": derivatives_score,
            "derivatives_reasons": derivatives_reasons,
            "plan": plan,
        }
    risk_pct, vol_target_reason = apply_aggressive_volatility_targeting(risk_pct, data.get("atr_pct"), cfg)
    if risk_pct <= 0:
        return {
            "enabled": True,
            "accepted": False,
            "reason": f"volatility target blocked: {vol_target_reason}",
            "growth_score": round(score, 2),
            "derivatives_score": derivatives_score,
            "derivatives_reasons": derivatives_reasons,
            "vol_target_reason": vol_target_reason,
            "plan": plan,
        }
    kelly_multiplier, kelly_reason = calculate_fractional_kelly_multiplier(
        data.get("recent_trades", data.get("aggressive_growth_recent_trades")),
        cfg,
    )
    risk_pct *= kelly_multiplier
    if risk_pct <= 0:
        return {
            "enabled": True,
            "accepted": False,
            "reason": f"kelly blocked: {kelly_reason}",
            "growth_score": round(score, 2),
            "derivatives_score": derivatives_score,
            "derivatives_reasons": derivatives_reasons,
            "vol_target_reason": vol_target_reason,
            "kelly_multiplier": kelly_multiplier,
            "kelly_reason": kelly_reason,
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

    planned_notional = qty * entry
    actual_risk = qty * risk_distance
    exit_split = choose_aggressive_exit_split(score, cfg)
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
        "aggressive_growth_sleeve_mode": cfg["aggressive_growth_sleeve_mode"],
        "aggressive_growth_sleeve_pct": sleeve_pct,
        "aggressive_growth_sleeve_cap_usdt": sleeve_cap,
        "aggressive_growth_available_sleeve_usdt": available_sleeve,
        "aggressive_growth_available_symbol_usdt": available_symbol,
        "aggressive_growth_qty_by_risk": qty_by_risk,
        "aggressive_growth_qty_by_sleeve": qty_by_sleeve,
        "aggressive_growth_qty_by_symbol": qty_by_symbol,
        "aggressive_growth_derivatives_score": derivatives_score,
        "aggressive_growth_derivatives_reasons": derivatives_reasons,
        "aggressive_growth_vol_target_reason": vol_target_reason,
        "aggressive_growth_kelly_multiplier": kelly_multiplier,
        "aggressive_growth_kelly_reason": kelly_reason,
        "aggressive_growth_exit_split_mode": exit_split["mode"],
        "partial_take_profit_enabled": True,
        "partial_take_profit_r_multiple": 1.0,
        "partial_take_profit_ratio": exit_split["tp1_pct"],
        "second_take_profit_enabled": exit_split["tp2_pct"] > 0,
        "second_take_profit_r_multiple": 2.0,
        "second_take_profit_ratio": exit_split["tp2_pct"],
        "runner_pct": exit_split["runner_pct"],
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
        "derivatives_score": derivatives_score,
        "derivatives_reasons": derivatives_reasons,
        "vol_target_reason": vol_target_reason,
        "kelly_multiplier": kelly_multiplier,
        "kelly_reason": kelly_reason,
        "exit_split": exit_split,
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
    if data.get("growth_context_valid") is False:
        reasons.append("growth context no longer valid")
    if data.get("symbol_trend_bullish") is False:
        reasons.append("symbol trend is not bullish")
    if data.get("volume_ok") is False:
        reasons.append("volume no longer confirms")
    if data.get("funding_not_overheated") is False:
        reasons.append("funding overheated")
    if data.get("daily_loss_limit_hit") is True:
        reasons.append("daily loss limit reached")
    if data.get("weekly_loss_limit_hit") is True:
        reasons.append("weekly loss limit reached")
    daily_pnl = _finite_float(data.get("daily_pnl_usdt"), 0.0)
    weekly_pnl = _finite_float(data.get("weekly_pnl_usdt"), 0.0)
    daily_limit = equity * cfg["aggressive_growth_daily_loss_limit_pct"]
    weekly_limit = equity * cfg["aggressive_growth_weekly_loss_limit_pct"]
    if equity > 0 and daily_limit > 0 and daily_pnl <= -daily_limit:
        reasons.append("daily loss limit reached")
    if equity > 0 and weekly_limit > 0 and weekly_pnl <= -weekly_limit:
        reasons.append("weekly loss limit reached")
    if reasons:
        return {"enabled": True, "accepted": False, "reason": "; ".join(reasons), "reasons": reasons}

    derivatives_score, derivatives_reasons = evaluate_derivatives_growth_score(data)
    if any(
        reason in {"funding_overheated", "long_crowded", "oi_up_price_stall", "oi_4h_up_price_stall"}
        for reason in derivatives_reasons
    ):
        reasons.append("derivatives context overheated")
    has_growth_inputs = any(
        key in data
        for key in (
            "growth_score",
            "htf_trend_bullish",
            "symbol_trend_bullish",
            "volume_ok",
            "adx_ok",
            "trend_strength_ok",
            "quality_ok",
            "funding_rate",
            "open_interest_change_1h",
            "open_interest_delta_pct",
            "price_change_1h",
            "long_short_ratio",
            "taker_buy_sell_ratio",
        )
    )
    score = _growth_score(data) if has_growth_inputs else None
    if score is not None and score < 50.0:
        reasons.append(f"growth score too low: {score:.1f}")
    if reasons:
        return {
            "enabled": True,
            "accepted": False,
            "reason": "; ".join(reasons),
            "reasons": reasons,
            "growth_score": round(score, 2) if score is not None else None,
            "derivatives_score": derivatives_score,
            "derivatives_reasons": derivatives_reasons,
        }

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
    risk_pct_cap = (
        cfg["aggressive_growth_max_trade_risk_pct_strong"]
        if score is not None and score >= 80.0
        else cfg["aggressive_growth_max_trade_risk_pct"]
    )
    max_loss = equity * risk_pct_cap if equity > 0 else float("inf")
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
        "growth_score": round(score, 2) if score is not None else None,
        "risk_pct_cap": risk_pct_cap,
        "derivatives_score": derivatives_score,
        "derivatives_reasons": derivatives_reasons,
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

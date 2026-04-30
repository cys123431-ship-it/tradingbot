"""Micro Auto V1 planning helpers.

The micro layer is intentionally pure. It does not place orders, mutate global
settings, or decide strategy signals. It only answers whether a tiny futures
account can safely execute an already accepted UT Breakout plan.
"""

from math import isfinite

from .risk import calculate_risk_plan


MICRO_AUTO_STRATEGY_KEY = "micro_auto_v1"

DEFAULT_MICRO_AUTO_CONFIG = {
    "enabled": False,
    "dry_run": True,
    "live_enabled": False,
    "equity_cap_usdt": 10.0,
    "min_equity_to_trade_usdt": 6.0,
    "min_leverage": 3,
    "max_leverage": 10,
    "max_margin_usage_pct": 45.0,
    "min_free_buffer_usdt": 1.5,
    "min_free_buffer_pct": 20.0,
    "risk_per_trade_pct": 1.5,
    "max_risk_usdt": 0.15,
    "daily_loss_limit_usdt": 0.45,
    "max_daily_trades": 3,
    "max_consecutive_losses": 2,
    "fee_rate_pct": 0.05,
    "slippage_pct": 0.03,
    "max_fee_burden_pct": 30.0,
    "high_fee_burden_pct": 20.0,
    "min_take_profit_r": 2.2,
    "high_fee_take_profit_r": 2.5,
    "preferred_timeframes": ["30m", "1h"],
    "allowed_timeframes": ["15m", "30m", "1h", "2h", "4h"],
    "max_active_positions": 1,
    "no_pyramiding": True,
    "reject_set_ids": [],
    "penalized_set_ids": [1, 49, 50],
}


def default_micro_auto_config():
    """Return a mutable copy of the Micro Auto defaults."""
    return {
        key: list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value
        for key, value in DEFAULT_MICRO_AUTO_CONFIG.items()
    }


def finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def finite_int(value, default=0):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return parsed


def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, finite_float(value, low)))


def normalize_micro_auto_config(raw=None):
    cfg = default_micro_auto_config()
    if isinstance(raw, dict):
        cfg.update(raw)

    def _bool_value(value, default=False):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return default

    for key in ("enabled", "dry_run", "live_enabled", "no_pyramiding"):
        cfg[key] = _bool_value(cfg.get(key), bool(DEFAULT_MICRO_AUTO_CONFIG.get(key, False)))

    for key in (
        "equity_cap_usdt",
        "min_equity_to_trade_usdt",
        "max_margin_usage_pct",
        "min_free_buffer_usdt",
        "min_free_buffer_pct",
        "risk_per_trade_pct",
        "max_risk_usdt",
        "daily_loss_limit_usdt",
        "fee_rate_pct",
        "slippage_pct",
        "max_fee_burden_pct",
        "high_fee_burden_pct",
        "min_take_profit_r",
        "high_fee_take_profit_r",
    ):
        default = DEFAULT_MICRO_AUTO_CONFIG[key]
        cfg[key] = max(0.0, finite_float(cfg.get(key), default))

    for key in ("min_leverage", "max_leverage", "max_daily_trades", "max_consecutive_losses", "max_active_positions"):
        default = DEFAULT_MICRO_AUTO_CONFIG[key]
        cfg[key] = max(0, finite_int(cfg.get(key), default))

    if cfg["min_leverage"] < 1:
        cfg["min_leverage"] = 1
    if cfg["max_leverage"] < cfg["min_leverage"]:
        cfg["max_leverage"] = cfg["min_leverage"]
    if cfg["equity_cap_usdt"] <= 0:
        cfg["equity_cap_usdt"] = DEFAULT_MICRO_AUTO_CONFIG["equity_cap_usdt"]
    if cfg["max_margin_usage_pct"] <= 0:
        cfg["max_margin_usage_pct"] = DEFAULT_MICRO_AUTO_CONFIG["max_margin_usage_pct"]
    if cfg["risk_per_trade_pct"] <= 0:
        cfg["risk_per_trade_pct"] = DEFAULT_MICRO_AUTO_CONFIG["risk_per_trade_pct"]
    if cfg["max_risk_usdt"] <= 0:
        cfg["max_risk_usdt"] = DEFAULT_MICRO_AUTO_CONFIG["max_risk_usdt"]
    if cfg["min_take_profit_r"] < 2.0:
        cfg["min_take_profit_r"] = 2.0
    if cfg["high_fee_take_profit_r"] < cfg["min_take_profit_r"]:
        cfg["high_fee_take_profit_r"] = cfg["min_take_profit_r"]

    for key in ("preferred_timeframes", "allowed_timeframes", "reject_set_ids", "penalized_set_ids"):
        value = cfg.get(key, [])
        if isinstance(value, str):
            cfg[key] = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(value, (list, tuple, set)):
            cfg[key] = [item for item in value]
        else:
            cfg[key] = list(DEFAULT_MICRO_AUTO_CONFIG.get(key, []))

    cfg["reject_set_ids"] = [finite_int(item, 0) for item in cfg.get("reject_set_ids", []) if finite_int(item, 0) > 0]
    cfg["penalized_set_ids"] = [finite_int(item, 0) for item in cfg.get("penalized_set_ids", []) if finite_int(item, 0) > 0]
    return cfg


def equity_for_micro(total_equity_usdt, free_usdt, cfg=None):
    cfg = normalize_micro_auto_config(cfg)
    total = finite_float(total_equity_usdt, 0.0)
    free = finite_float(free_usdt, 0.0)
    usable_total = total if total > 0 else free
    return min(max(0.0, usable_total), cfg["equity_cap_usdt"])


def micro_margin_cap_usdt(equity_usdt, cfg=None):
    cfg = normalize_micro_auto_config(cfg)
    return equity_for_micro(equity_usdt, equity_usdt, cfg) * cfg["max_margin_usage_pct"] / 100.0


def estimate_roundtrip_cost_usdt(notional_usdt, cfg=None):
    cfg = normalize_micro_auto_config(cfg)
    notional = max(0.0, finite_float(notional_usdt, 0.0))
    # Entry + exit for both commission and expected slippage.
    roundtrip_pct = (cfg["fee_rate_pct"] * 2.0) + (cfg["slippage_pct"] * 2.0)
    return notional * roundtrip_pct / 100.0


def liquidation_proxy_ok(stop_distance_pct, leverage, safety_multiple=3.0, buffer_pct=1.0):
    """Approximate whether the stop sits well before the liquidation zone.

    This is a conservative proxy, not an exchange liquidation calculator. The
    real liquidation price depends on maintenance margin, wallet balance and
    position mode. For micro accounts we only need an early safety veto.
    """
    stop_pct = max(0.0, finite_float(stop_distance_pct, 0.0))
    lev = max(1.0, finite_float(leverage, 1.0))
    approx_liq_move_pct = 100.0 / lev
    required_move_pct = (stop_pct * safety_multiple) + buffer_pct
    return approx_liq_move_pct >= required_move_pct


def choose_micro_leverage(
    *,
    total_equity_usdt,
    free_usdt,
    min_notional_usdt,
    stop_distance_pct,
    cfg=None,
    max_symbol_leverage=None,
):
    cfg = normalize_micro_auto_config(cfg)
    equity = equity_for_micro(total_equity_usdt, free_usdt, cfg)
    free = finite_float(free_usdt, 0.0)
    min_notional = max(0.0, finite_float(min_notional_usdt, 0.0))
    max_symbol_lev = finite_int(max_symbol_leverage, cfg["max_leverage"])
    max_lev = max(cfg["min_leverage"], min(cfg["max_leverage"], max_symbol_lev))
    margin_cap = equity * cfg["max_margin_usage_pct"] / 100.0
    free_buffer = max(cfg["min_free_buffer_usdt"], equity * cfg["min_free_buffer_pct"] / 100.0)

    if equity < cfg["min_equity_to_trade_usdt"]:
        return {
            "accepted": False,
            "reject_code": "REJECTED_MICRO_EQUITY_LOW",
            "reason": f"micro equity {equity:.2f} < {cfg['min_equity_to_trade_usdt']:.2f}",
            "equity_for_micro": equity,
        }
    if min_notional <= 0:
        return {
            "accepted": False,
            "reject_code": "REJECTED_MICRO_MIN_NOTIONAL_UNKNOWN",
            "reason": "exchange minNotional is unknown",
            "equity_for_micro": equity,
        }

    attempts = []
    for lev in range(cfg["min_leverage"], max_lev + 1):
        required_margin = min_notional / max(float(lev), 1e-9)
        free_after = free - required_margin
        ok_margin = required_margin <= margin_cap + 1e-12
        ok_free = free_after >= free_buffer - 1e-12
        ok_liq = liquidation_proxy_ok(stop_distance_pct, lev)
        attempts.append({
            "leverage": lev,
            "required_margin": required_margin,
            "free_after_entry": free_after,
            "margin_cap": margin_cap,
            "free_buffer": free_buffer,
            "liquidation_proxy_ok": ok_liq,
            "accepted": ok_margin and ok_free and ok_liq,
        })
        if ok_margin and ok_free and ok_liq:
            return {
                "accepted": True,
                "leverage": lev,
                "required_margin": required_margin,
                "margin_cap": margin_cap,
                "free_buffer": free_buffer,
                "free_after_entry": free_after,
                "equity_for_micro": equity,
                "attempts": attempts,
            }

    best = attempts[-1] if attempts else {}
    return {
        "accepted": False,
        "reject_code": "REJECTED_MICRO_MIN_NOTIONAL_MARGIN",
        "reason": (
            f"minNotional {min_notional:.2f} cannot fit margin cap "
            f"{margin_cap:.2f} USDT up to {max_lev}x"
        ),
        "equity_for_micro": equity,
        "attempts": attempts,
        "last_attempt": best,
    }


def assess_micro_market_feasibility(
    *,
    symbol,
    total_equity_usdt,
    free_usdt,
    min_notional_usdt,
    cfg=None,
    assumed_stop_distance_pct=1.5,
    max_symbol_leverage=None,
):
    decision = choose_micro_leverage(
        total_equity_usdt=total_equity_usdt,
        free_usdt=free_usdt,
        min_notional_usdt=min_notional_usdt,
        stop_distance_pct=assumed_stop_distance_pct,
        cfg=cfg,
        max_symbol_leverage=max_symbol_leverage,
    )
    decision.update({
        "symbol": symbol,
        "min_notional_usdt": max(0.0, finite_float(min_notional_usdt, 0.0)),
        "assumed_stop_distance_pct": assumed_stop_distance_pct,
    })
    return decision


def score_micro_set(set_info=None, auto_scores=None, cfg=None):
    cfg = normalize_micro_auto_config(cfg)
    set_info = set_info or {}
    auto_scores = auto_scores or {}
    set_id = finite_int(set_info.get("id") or auto_scores.get("auto_final_set_id"), 0)
    family = str(set_info.get("family") or "").lower()
    score = 50.0
    reasons = []

    confidence = str(auto_scores.get("auto_confidence") or "").lower()
    margin = finite_float(auto_scores.get("auto_score_margin"), 0.0)
    selected_score = finite_float(auto_scores.get("auto_selected_score"), 0.0)
    score += clamp(selected_score, 0.0, 100.0) * 0.18
    score += clamp(margin, 0.0, 20.0) * 0.9

    if confidence in {"strong", "high"}:
        score += 8.0
    elif confidence in {"weak", "low"}:
        score -= 8.0

    if set_id in cfg.get("reject_set_ids", []):
        score -= 100.0
        reasons.append("manual reject set")
    if set_id in cfg.get("penalized_set_ids", []):
        score -= 14.0
        reasons.append("micro penalty: fallback/emergency/simple set")
    if family in {"trend strength", "multi-timeframe alignment", "pullback/continuation"}:
        score += 8.0
    if family in {"special regime", "ut core"}:
        score -= 5.0
    if family in {"breakout", "momentum"}:
        score += 2.0

    return {
        "set_id": set_id,
        "score": round(clamp(score, 0.0, 100.0), 2),
        "family": set_info.get("family"),
        "name": set_info.get("name"),
        "reasons": reasons,
    }


def build_micro_entry_plan(
    *,
    side,
    entry_price,
    atr_value,
    ut_stop=None,
    base_plan=None,
    cfg=None,
    selected_set=None,
    auto_scores=None,
    selected_timeframe=None,
    total_equity_usdt=0.0,
    free_usdt=0.0,
    min_notional_usdt=0.0,
    max_symbol_leverage=None,
):
    """Create a Micro Auto entry plan from an accepted UT Breakout candidate."""
    cfg = normalize_micro_auto_config(cfg)
    base_plan = dict(base_plan or {})
    selected_set = dict(selected_set or {})
    set_score = score_micro_set(selected_set, auto_scores, cfg)

    side = str(side or "").lower()
    entry = finite_float(entry_price, 0.0)
    atr = finite_float(atr_value, 0.0)
    if side not in {"long", "short"}:
        return {"accepted": False, "reject_code": "REJECTED_MICRO_SIDE", "reason": "side must be long or short"}
    if entry <= 0 or atr <= 0:
        return {"accepted": False, "reject_code": "REJECTED_MICRO_PRICE_ATR", "reason": "entry/ATR unavailable"}

    equity = equity_for_micro(total_equity_usdt, free_usdt, cfg)
    if equity < cfg["min_equity_to_trade_usdt"]:
        return {
            "accepted": False,
            "reject_code": "REJECTED_MICRO_EQUITY_LOW",
            "reason": f"micro equity {equity:.2f} < {cfg['min_equity_to_trade_usdt']:.2f}",
            "equity_for_micro": equity,
        }

    stop_mult = finite_float(base_plan.get("stop_atr_multiplier"), None)
    if stop_mult is None:
        stop_mult = finite_float(selected_set.get("params", {}).get("stop_atr_multiplier"), 1.5)
    base_rr = max(
        finite_float(base_plan.get("rr_multiple"), 2.0),
        finite_float(selected_set.get("params", {}).get("take_profit_r_multiple"), 2.0),
        cfg["min_take_profit_r"],
    )

    stop_anchor_distance = abs(entry - finite_float(ut_stop, entry)) if ut_stop is not None else 0.0
    preliminary_risk_distance = max(stop_mult * atr, stop_anchor_distance)
    if preliminary_risk_distance <= 0:
        return {"accepted": False, "reject_code": "REJECTED_MICRO_RISK_DISTANCE", "reason": "risk distance unavailable"}
    stop_distance_pct = preliminary_risk_distance / entry * 100.0

    leverage_decision = choose_micro_leverage(
        total_equity_usdt=total_equity_usdt,
        free_usdt=free_usdt,
        min_notional_usdt=min_notional_usdt,
        stop_distance_pct=stop_distance_pct,
        cfg=cfg,
        max_symbol_leverage=max_symbol_leverage,
    )
    if not leverage_decision.get("accepted"):
        leverage_decision.update({
            "selected_set": set_score,
            "selected_timeframe": selected_timeframe,
        })
        return leverage_decision

    leverage = int(leverage_decision["leverage"])
    risk_budget = min(
        equity * cfg["risk_per_trade_pct"] / 100.0,
        cfg["max_risk_usdt"],
    )
    try:
        risk_plan = calculate_risk_plan(
            side=side,
            entry_price=entry,
            atr_value=atr,
            stop_atr_multiplier=stop_mult,
            ut_stop=ut_stop,
            take_profit_r_multiple=base_rr,
            min_risk_reward=2.0,
            balance_usdt=equity,
            risk_per_trade_percent=cfg["risk_per_trade_pct"],
            max_risk_per_trade_usdt=cfg["max_risk_usdt"],
            leverage=leverage,
        )
    except ValueError as exc:
        return {"accepted": False, "reject_code": "REJECTED_MICRO_RISK_PLAN", "reason": str(exc)}

    min_notional = max(0.0, finite_float(min_notional_usdt, 0.0))
    planned_notional = max(float(risk_plan["planned_notional"]), min_notional * 1.001)
    actual_qty = planned_notional / entry
    actual_risk_usdt = actual_qty * float(risk_plan["risk_distance"])
    if actual_risk_usdt > risk_budget + 1e-9:
        return {
            "accepted": False,
            "reject_code": "REJECTED_MICRO_MIN_NOTIONAL_RISK",
            "reason": (
                f"minNotional bump risk {actual_risk_usdt:.4f} > "
                f"budget {risk_budget:.4f} USDT"
            ),
            "risk_budget_usdt": risk_budget,
            "min_notional_usdt": min_notional,
            "selected_set": set_score,
            "selected_timeframe": selected_timeframe,
        }

    fee_cost = estimate_roundtrip_cost_usdt(planned_notional, cfg)
    fee_burden_pct = fee_cost / max(actual_risk_usdt, 1e-9) * 100.0
    if fee_burden_pct > cfg["max_fee_burden_pct"]:
        return {
            "accepted": False,
            "reject_code": "REJECTED_MICRO_FEE_BURDEN",
            "reason": f"roundtrip cost/risk {fee_burden_pct:.1f}% > {cfg['max_fee_burden_pct']:.1f}%",
            "fee_burden_pct": fee_burden_pct,
            "estimated_roundtrip_cost_usdt": fee_cost,
            "selected_set": set_score,
            "selected_timeframe": selected_timeframe,
        }

    rr = base_rr
    if fee_burden_pct >= cfg["high_fee_burden_pct"]:
        rr = max(rr, cfg["high_fee_take_profit_r"])

    risk_distance = float(risk_plan["risk_distance"])
    if side == "long":
        stop_loss = entry - risk_distance
        take_profit = entry + (risk_distance * rr)
    else:
        stop_loss = entry + risk_distance
        take_profit = entry - (risk_distance * rr)

    planned_margin = planned_notional / max(float(leverage), 1e-9)
    if planned_margin > leverage_decision["margin_cap"] + 1e-9:
        return {
            "accepted": False,
            "reject_code": "REJECTED_MICRO_MARGIN_CAP",
            "reason": f"margin {planned_margin:.4f} > cap {leverage_decision['margin_cap']:.4f}",
            "selected_set": set_score,
            "selected_timeframe": selected_timeframe,
        }

    return {
        "accepted": True,
        "micro_auto": True,
        "micro_strategy": MICRO_AUTO_STRATEGY_KEY,
        "dry_run": bool(cfg["dry_run"]),
        "live_enabled": bool(cfg["live_enabled"]),
        "side": side,
        "entry_price": entry,
        "risk_distance": risk_distance,
        "risk_distance_pct": risk_distance / entry * 100.0,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "take_profit_distance": risk_distance * rr,
        "take_profit_pct": (risk_distance * rr) / entry * 100.0,
        "risk_usdt": actual_risk_usdt,
        "risk_budget_usdt": risk_budget,
        "qty": actual_qty,
        "planned_notional": planned_notional,
        "planned_margin": planned_margin,
        "leverage": leverage,
        "rr_multiple": rr,
        "expected_profit_usdt": actual_risk_usdt * rr,
        "min_notional_usdt": min_notional,
        "equity_for_micro": equity,
        "free_usdt": finite_float(free_usdt, 0.0),
        "margin_cap_usdt": leverage_decision["margin_cap"],
        "free_buffer_usdt": leverage_decision["free_buffer"],
        "free_after_entry_usdt": finite_float(free_usdt, 0.0) - planned_margin,
        "estimated_roundtrip_cost_usdt": fee_cost,
        "fee_burden_pct": fee_burden_pct,
        "selected_set": set_score,
        "selected_set_id": set_score.get("set_id"),
        "selected_set_name": set_score.get("name"),
        "selected_set_family": set_score.get("family"),
        "selected_timeframe": selected_timeframe,
        "leverage_attempts": leverage_decision.get("attempts", []),
    }

"""Risk plan helpers for UT Breakout.

These functions are intentionally dependency-free so they can be tested without
exchange, Telegram, or pandas state.
"""

from math import isfinite

DEFAULT_RISK_PER_TRADE_PERCENT = 0.5
DEFAULT_MIN_RISK_PER_TRADE_PERCENT = 0.05
DEFAULT_MAX_RISK_PER_TRADE_PERCENT = 1.0


def _finite_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def normalize_risk_percent(
    config_or_value=None,
    *,
    default=DEFAULT_RISK_PER_TRADE_PERCENT,
    min_default=DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
    max_default=DEFAULT_MAX_RISK_PER_TRADE_PERCENT,
):
    """Return a bounded percent risk value for live futures sizing."""
    cfg = config_or_value if isinstance(config_or_value, dict) else {}
    if isinstance(config_or_value, dict):
        raw = cfg.get("risk_per_trade_percent", cfg.get("risk_per_trade_pct", default))
    else:
        raw = config_or_value if config_or_value not in (None, "") else default

    min_raw = cfg.get(
        "min_risk_per_trade_percent",
        cfg.get("min_risk_per_trade_pct", min_default),
    )
    max_raw = cfg.get(
        "max_risk_per_trade_percent",
        cfg.get("max_risk_per_trade_pct", max_default),
    )
    min_pct = max(0.0, _finite_float(min_raw, min_default))
    max_pct = max(min_pct, min(_finite_float(max_raw, max_default), float(max_default)))
    parsed = _finite_float(raw, default)
    if parsed is None or parsed <= 0:
        parsed = _finite_float(default, DEFAULT_RISK_PER_TRADE_PERCENT)
    return max(min_pct, min(max_pct, parsed))


def calculate_risk_plan(
    *,
    side,
    entry_price,
    atr_value,
    stop_atr_multiplier=1.5,
    ut_stop=None,
    structure_stop=None,
    structure_buffer_atr=0.0,
    take_profit_r_multiple=2.0,
    take_profit_front_run_atr=0.0,
    take_profit_front_run_pct=0.0,
    min_risk_reward=2.0,
    balance_usdt=0.0,
    risk_per_trade_percent=DEFAULT_RISK_PER_TRADE_PERCENT,
    max_risk_per_trade_usdt=1.0,
    leverage=1.0,
):
    """Build a pre-entry risk plan from stop-loss budget, not leverage.

    The notional/margin fields are informational. The controlling value is
    risk_usdt, which is the amount lost if stop_loss is hit.
    """
    side = str(side or "").lower()
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")

    entry = _finite_float(entry_price)
    atr = _finite_float(atr_value)
    if entry is None or entry <= 0:
        raise ValueError("entry_price must be positive")
    if atr is None or atr <= 0:
        raise ValueError("atr_value must be positive")

    stop_mult = max(0.0, _finite_float(stop_atr_multiplier, 1.5))
    rr = _finite_float(take_profit_r_multiple, 2.0)
    min_rr = _finite_float(min_risk_reward, 2.0)
    if rr is None or min_rr is None or rr < min_rr:
        raise ValueError("risk reward is below the configured minimum")

    ut_stop_value = _finite_float(ut_stop)
    stop_anchor_distance = abs(entry - ut_stop_value) if ut_stop_value is not None else 0.0
    structure_stop_value = _finite_float(structure_stop)
    structure_buffer = max(0.0, _finite_float(structure_buffer_atr, 0.0)) * atr
    structure_stop_with_buffer = None
    structure_anchor_distance = 0.0
    soft_stop_candidates = []
    if side == "long":
        if ut_stop_value is not None and ut_stop_value < entry:
            soft_stop_candidates.append(ut_stop_value)
        if structure_stop_value is not None and structure_stop_value < entry:
            structure_stop_with_buffer = structure_stop_value - structure_buffer
            structure_anchor_distance = max(0.0, entry - structure_stop_with_buffer)
            soft_stop_candidates.append(structure_stop_value)
    else:
        if ut_stop_value is not None and ut_stop_value > entry:
            soft_stop_candidates.append(ut_stop_value)
        if structure_stop_value is not None and structure_stop_value > entry:
            structure_stop_with_buffer = structure_stop_value + structure_buffer
            structure_anchor_distance = max(0.0, structure_stop_with_buffer - entry)
            soft_stop_candidates.append(structure_stop_value)

    risk_distance = max(stop_mult * atr, stop_anchor_distance, structure_anchor_distance)
    if risk_distance <= 0:
        raise ValueError("risk_distance must be positive")

    requested_tp_distance = rr * risk_distance
    front_run_distance = max(
        max(0.0, _finite_float(take_profit_front_run_atr, 0.0)) * atr,
        requested_tp_distance * max(0.0, _finite_float(take_profit_front_run_pct, 0.0)),
    )
    max_front_run = max(0.0, requested_tp_distance - (min_rr * risk_distance))
    front_run_distance = min(front_run_distance, max_front_run)
    effective_tp_distance = requested_tp_distance - front_run_distance
    effective_rr = effective_tp_distance / risk_distance

    if side == "long":
        stop_loss = entry - risk_distance
        soft_stop_loss = max(soft_stop_candidates) if soft_stop_candidates else None
        take_profit = entry + effective_tp_distance
    else:
        stop_loss = entry + risk_distance
        soft_stop_loss = min(soft_stop_candidates) if soft_stop_candidates else None
        take_profit = entry - effective_tp_distance

    balance = max(0.0, _finite_float(balance_usdt, 0.0))
    risk_pct = max(0.0, _finite_float(risk_per_trade_percent, 0.0))
    percent_budget = balance * risk_pct / 100.0
    max_budget = _finite_float(max_risk_per_trade_usdt)
    risk_usdt = min(percent_budget, max_budget) if max_budget and max_budget > 0 else percent_budget
    if risk_usdt <= 0:
        raise ValueError("risk budget unavailable")

    qty = risk_usdt / risk_distance
    notional = qty * entry
    lev = max(1.0, _finite_float(leverage, 1.0))
    margin = notional / lev
    return {
        "side": side,
        "entry_price": entry,
        "risk_distance": risk_distance,
        "risk_distance_pct": risk_distance / entry * 100.0,
        "stop_loss": stop_loss,
        "hard_stop_loss": stop_loss,
        "soft_stop_loss": soft_stop_loss,
        "take_profit": take_profit,
        "take_profit_distance": effective_tp_distance,
        "take_profit_pct": effective_tp_distance / entry * 100.0,
        "requested_take_profit_distance": requested_tp_distance,
        "take_profit_front_run_distance": front_run_distance,
        "risk_usdt": risk_usdt,
        "qty": qty,
        "planned_notional": notional,
        "planned_margin": margin,
        "leverage": lev,
        "rr_multiple": rr,
        "effective_rr_multiple": effective_rr,
        "expected_profit_usdt": risk_usdt * effective_rr,
        "stop_anchor_distance": stop_anchor_distance,
        "structure_stop": structure_stop_value,
        "structure_stop_with_buffer": structure_stop_with_buffer,
        "structure_anchor_distance": structure_anchor_distance,
        "structure_buffer_atr": max(0.0, _finite_float(structure_buffer_atr, 0.0)),
    }

"""Risk plan helpers for UT Breakout.

These functions are intentionally dependency-free so they can be tested without
exchange, Telegram, or pandas state.
"""

from math import isfinite


def _finite_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def calculate_risk_plan(
    *,
    side,
    entry_price,
    atr_value,
    stop_atr_multiplier=1.5,
    ut_stop=None,
    take_profit_r_multiple=2.0,
    min_risk_reward=2.0,
    balance_usdt=0.0,
    risk_per_trade_percent=1.0,
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
    risk_distance = max(stop_mult * atr, stop_anchor_distance)
    if risk_distance <= 0:
        raise ValueError("risk_distance must be positive")

    if side == "long":
        stop_loss = entry - risk_distance
        take_profit = entry + (rr * risk_distance)
    else:
        stop_loss = entry + risk_distance
        take_profit = entry - (rr * risk_distance)

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
        "take_profit": take_profit,
        "take_profit_distance": rr * risk_distance,
        "take_profit_pct": (rr * risk_distance) / entry * 100.0,
        "risk_usdt": risk_usdt,
        "qty": qty,
        "planned_notional": notional,
        "planned_margin": margin,
        "leverage": lev,
        "rr_multiple": rr,
        "expected_profit_usdt": risk_usdt * rr,
        "stop_anchor_distance": stop_anchor_distance,
    }

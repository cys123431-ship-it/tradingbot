"""UTBreakout risk-budget and order-quantity reconciliation helpers."""

from __future__ import annotations

import math
from typing import Any

from .risk import (
    DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
    DEFAULT_RISK_PER_TRADE_PERCENT,
    normalize_risk_percent,
)


UTBREAKOUT_MAX_RISK_PER_TRADE_PERCENT = 10.0
UTBREAKOUT_READY_AGE_MIN_SECONDS_BY_TIMEFRAME = {
    "15m": 600.0,
    "30m": 900.0,
    "1h": 1200.0,
    "4h": 1800.0,
    "6h": 2700.0,
}


def _optional_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def resolve_utbreakout_risk_budget(
    balance_usdt,
    cfg,
    *,
    multiplier=1.0,
    daily_pnl_usdt=None,
):
    """Resolve the UT/RSPT/aggregate loss budget from account percentage."""

    cfg = cfg if isinstance(cfg, dict) else {}
    balance = max(0.0, float(balance_usdt or 0.0))
    risk_percent = normalize_risk_percent(
        {
            "risk_per_trade_percent": cfg.get(
                "risk_per_trade_percent",
                cfg.get("risk_per_trade_pct", DEFAULT_RISK_PER_TRADE_PERCENT),
            ),
            "min_risk_per_trade_percent": cfg.get(
                "min_risk_per_trade_percent",
                cfg.get(
                    "min_risk_per_trade_pct",
                    DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                ),
            ),
            "max_risk_per_trade_percent": cfg.get(
                "max_risk_per_trade_percent",
                cfg.get(
                    "max_risk_per_trade_pct",
                    UTBREAKOUT_MAX_RISK_PER_TRADE_PERCENT,
                ),
            ),
        },
        max_default=UTBREAKOUT_MAX_RISK_PER_TRADE_PERCENT,
    )
    tracks_equity = bool(cfg.get("risk_budget_tracks_account_equity", True))
    if tracks_equity:
        max_risk_usdt = balance * risk_percent / 100.0
        hard_cap = max(
            0.0,
            float(cfg.get("max_risk_per_trade_usdt_hard_cap", 0.0) or 0.0),
        )
        if hard_cap > 0:
            max_risk_usdt = min(max_risk_usdt, hard_cap)
        source = "account_equity_percent"
    else:
        max_risk_usdt = max(
            0.0,
            float(cfg.get("max_risk_per_trade_usdt", 1.0) or 0.0),
        )
        source = "fixed_usdt_cap"

    daily_loss_cap = max(
        0.0,
        float(cfg.get("daily_max_loss_usdt", 0.0) or 0.0),
    )
    daily_pnl = _optional_number(daily_pnl_usdt)
    daily_remaining_loss_usdt = daily_loss_cap
    if daily_loss_cap > 0 and daily_pnl is not None and daily_pnl < 0:
        daily_remaining_loss_usdt = max(0.0, daily_loss_cap + daily_pnl)
    daily_cap_applied = (
        daily_loss_cap > 0
        and max_risk_usdt > daily_remaining_loss_usdt
    )
    if daily_loss_cap > 0:
        max_risk_usdt = min(max_risk_usdt, daily_remaining_loss_usdt)

    applied_multiplier = max(0.0, float(multiplier or 0.0))
    return {
        "risk_per_trade_percent": risk_percent * applied_multiplier,
        "max_risk_per_trade_usdt": max_risk_usdt * applied_multiplier,
        "base_risk_per_trade_percent": risk_percent,
        "base_max_risk_per_trade_usdt": max_risk_usdt,
        "multiplier": applied_multiplier,
        "source": source,
        "daily_loss_cap_usdt": daily_loss_cap,
        "daily_pnl_usdt": daily_pnl,
        "daily_remaining_loss_usdt": daily_remaining_loss_usdt,
        "daily_loss_cap_applied": daily_cap_applied,
    }


def cap_utbreakout_risk_plan_to_margin(
    plan,
    *,
    free_balance,
    leverage,
    entry_price,
    safety_buffer=0.98,
):
    """Cap a risk plan to available isolated margin without rejecting it."""

    plan = dict(plan or {})
    price = float(entry_price or 0.0)
    free = max(0.0, float(free_balance or 0.0))
    lev = max(1.0, float(leverage or 1.0))
    max_notional = free * lev * max(
        0.0,
        min(1.0, float(safety_buffer or 0.0)),
    )
    planned_notional = max(
        0.0,
        float(plan.get("planned_notional", 0.0) or 0.0),
    )
    if price <= 0 or max_notional <= 0 or planned_notional <= max_notional:
        plan.setdefault("position_cap_applied", False)
        return plan

    capped_qty = max_notional / price
    risk_distance = max(
        0.0,
        float(plan.get("risk_distance", 0.0) or 0.0),
    )
    original_notional = planned_notional
    original_risk = max(0.0, float(plan.get("risk_usdt", 0.0) or 0.0))
    plan.update(
        {
            "qty": capped_qty,
            "planned_notional": max_notional,
            "planned_margin": max_notional / lev,
            "risk_usdt": capped_qty * risk_distance,
            "expected_profit_usdt": capped_qty
            * risk_distance
            * float(
                plan.get(
                    "effective_rr_multiple",
                    plan.get("rr_multiple", 0.0),
                )
                or 0.0
            ),
            "position_cap_applied": True,
            "position_cap_reason": "available_margin_and_leverage",
            "position_cap_original_notional": original_notional,
            "position_cap_original_risk_usdt": original_risk,
            "position_cap_max_notional": max_notional,
        }
    )
    return plan


def reconcile_utbreakout_risk_plan_to_order_qty(
    plan,
    *,
    qty,
    entry_price,
    leverage,
    reason="exchange_amount_precision",
):
    """Keep stored risk diagnostics aligned with submitted quantity."""

    reconciled = dict(plan or {})
    actual_qty = max(0.0, float(qty or 0.0))
    price = max(0.0, float(entry_price or 0.0))
    lev = max(1.0, float(leverage or 1.0))
    actual_notional = actual_qty * price
    risk_distance = max(
        0.0,
        float(reconciled.get("risk_distance", 0.0) or 0.0),
    )
    rr_multiple = float(
        reconciled.get(
            "effective_rr_multiple",
            reconciled.get("rr_multiple", 0.0),
        )
        or 0.0
    )
    previous_qty = max(0.0, float(reconciled.get("qty", 0.0) or 0.0))
    previous_notional = max(
        0.0,
        float(
            reconciled.get("planned_notional", previous_qty * price) or 0.0
        ),
    )
    reconciled.update(
        {
            "qty": actual_qty,
            "planned_notional": actual_notional,
            "planned_margin": actual_notional / lev,
            "risk_usdt": actual_qty * risk_distance,
            "expected_profit_usdt": actual_qty
            * risk_distance
            * rr_multiple,
            "leverage": int(lev),
            "order_qty_reconciled": True,
            "order_qty_reconcile_reason": str(reason or "order_sizing"),
            "order_qty_before_reconcile": previous_qty,
            "order_notional_before_reconcile": previous_notional,
        }
    )
    return reconciled


def resolve_utbreakout_bridge_ready_age_sec(cfg, entry_timeframe=None):
    """Keep bridge freshness compatible with multi-timeframe scans."""

    cfg = cfg if isinstance(cfg, dict) else {}
    configured = max(
        0.0,
        float(
            cfg.get(
                "utbreakout_auto_entry_bridge_max_ready_age_sec",
                180.0,
            )
            or 180.0
        ),
    )
    timeframe = str(
        entry_timeframe or cfg.get("entry_timeframe") or "15m"
    ).strip().lower()
    minimum = UTBREAKOUT_READY_AGE_MIN_SECONDS_BY_TIMEFRAME.get(
        timeframe,
        600.0,
    )
    return max(configured, minimum)

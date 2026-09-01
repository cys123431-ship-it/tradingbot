"""Pure stop-loss geometry rules for live small-account protection."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class StopGeometry:
    valid: bool
    crossed_live_mark: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ProfitBankRiskBudget:
    """Bounded next-entry risk after a profitable trading day.

    This is deliberately a sizing result, not a daily stop.  The strategy can
    continue trading, while a configurable share of already-realized profit is
    kept outside the next position's initial stop budget.
    """

    enabled: bool
    active: bool
    risk_scale: float
    normal_initial_risk_usdt: float
    effective_initial_risk_usdt: float
    activation_profit_usdt: float
    protected_profit_usdt: float
    available_giveback_usdt: float
    reason: str


def resolve_profit_bank_risk_budget(
    *,
    account_equity: Any,
    daily_realized_pnl_usdt: Any,
    normal_full_risk_usdt: Any,
    initial_fraction: Any,
    enabled: bool = True,
    activation_multiple: Any = 0.75,
    protect_fraction: Any = 0.50,
    minimum_risk_scale: Any = 0.50,
) -> ProfitBankRiskBudget:
    """Scale one future entry without halting a profitable small account.

    ``normal_full_risk_usdt`` is the already-selected campaign loss budget
    after the stability overlay.  The returned scale is applied once to that
    budget; the existing initial-entry fraction then produces the effective
    initial stop budget represented here.
    """

    equity = _nonnegative_float(account_equity)
    daily_pnl = _finite_float(daily_realized_pnl_usdt, 0.0)
    full_risk = _nonnegative_float(normal_full_risk_usdt)
    initial = _bounded_float(initial_fraction, 0.01, 1.0, 0.65)
    activation = _bounded_float(activation_multiple, 0.10, 3.0, 0.75)
    protect = _bounded_float(protect_fraction, 0.0, 0.95, 0.50)
    floor_scale = _bounded_float(minimum_risk_scale, 0.10, 1.0, 0.50)
    normal_initial = full_risk * initial
    activation_profit = normal_initial * activation

    base = {
        "enabled": bool(enabled),
        "active": False,
        "risk_scale": 1.0,
        "normal_initial_risk_usdt": normal_initial,
        "effective_initial_risk_usdt": normal_initial,
        "activation_profit_usdt": activation_profit,
        "protected_profit_usdt": 0.0,
        "available_giveback_usdt": 0.0,
    }
    if not enabled:
        return ProfitBankRiskBudget(
            **base,
            reason="profit bank disabled",
        )
    if equity <= 0.0 or normal_initial <= 0.0:
        return ProfitBankRiskBudget(
            **base,
            reason="profit bank unavailable: invalid equity or risk budget",
        )
    if daily_pnl < activation_profit:
        return ProfitBankRiskBudget(
            **base,
            reason=(
                f"profit bank waiting: daily={daily_pnl:.2f} "
                f"activation={activation_profit:.2f}"
            ),
        )

    protected_profit = daily_pnl * protect
    available_giveback = max(0.0, daily_pnl - protected_profit)
    minimum_initial = normal_initial * floor_scale
    effective_initial = min(
        normal_initial,
        max(minimum_initial, available_giveback),
    )
    risk_scale = max(
        floor_scale,
        min(1.0, effective_initial / normal_initial),
    )
    return ProfitBankRiskBudget(
        enabled=True,
        active=risk_scale < 1.0 - 1e-9,
        risk_scale=risk_scale,
        normal_initial_risk_usdt=normal_initial,
        effective_initial_risk_usdt=normal_initial * risk_scale,
        activation_profit_usdt=activation_profit,
        protected_profit_usdt=protected_profit,
        available_giveback_usdt=available_giveback,
        reason=(
            f"profit bank x{risk_scale:.2f}: daily={daily_pnl:.2f}, "
            f"protected={protected_profit:.2f}, "
            f"nextInitialRisk={normal_initial * risk_scale:.2f}"
        ),
    )


def position_mark_price(position: Mapping[str, Any] | None) -> float | None:
    """Read the exchange mark from normalized or raw position fields."""

    if not isinstance(position, Mapping):
        return None
    info = position.get("info") if isinstance(position.get("info"), Mapping) else {}
    for value in (
        position.get("markPrice"),
        position.get("mark_price"),
        info.get("markPrice"),
        info.get("mark_price"),
        position.get("lastPrice"),
        info.get("lastPrice"),
    ):
        parsed = _positive_float(value)
        if parsed is not None:
            return parsed
    return None


def classify_stop_geometry(
    *,
    side: str,
    stop_price: Any,
    entry_price: Any,
    mark_price: Any = None,
    bot_managed: bool,
) -> StopGeometry:
    """Classify a stop without confusing profit protection with invalid SL.

    A bot-managed winner stop may correctly sit above a long entry (or below a
    short entry).  Once accepted by the exchange it is preserved even if the
    latest polled mark has crossed it; cancelling at that instant creates an
    unprotected gap exactly when the exchange trigger should be allowed to
    execute.  ``crossed_live_mark`` is diagnostic, not a cancellation signal.
    """

    normalized_side = str(side or "").strip().lower()
    stop = _positive_float(stop_price)
    entry = _positive_float(entry_price)
    mark = _positive_float(mark_price)
    if normalized_side not in {"long", "short"}:
        return StopGeometry(False, reason="INVALID_POSITION_SIDE")
    if stop is None:
        return StopGeometry(False, reason="INVALID_STOP_PRICE")

    if bot_managed:
        crossed = bool(
            mark is not None
            and (
                stop >= mark
                if normalized_side == "long"
                else stop <= mark
            )
        )
        return StopGeometry(
            True,
            crossed_live_mark=crossed,
            reason=(
                "MANAGED_STOP_TRIGGER_PENDING"
                if crossed
                else "MANAGED_STOP_VALID"
            ),
        )

    if entry is None:
        return StopGeometry(False, reason="ENTRY_PRICE_UNAVAILABLE")
    valid = stop < entry if normalized_side == "long" else stop > entry
    return StopGeometry(
        valid,
        reason="EXTERNAL_STOP_VALID" if valid else "EXTERNAL_STOP_WRONG_ENTRY_SIDE",
    )


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _finite_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _nonnegative_float(value: Any) -> float:
    return max(0.0, _finite_float(value, 0.0))


def _bounded_float(value: Any, lower: float, upper: float, default: float) -> float:
    return max(lower, min(upper, _finite_float(value, default)))

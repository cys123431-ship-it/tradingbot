"""Hard capital controls for long-premium option orders."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from .config import OPTIONS_CAPITAL_LIMIT_USDT


def _d(value, default="0"):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(str(default))


def floor_to_step(value, step):
    value_d = max(Decimal("0"), _d(value))
    step_d = _d(step)
    if step_d <= 0:
        return value_d
    return (value_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d


def estimate_option_fee(option_price, index_price, quantity, unit=1):
    """Conservative Binance crypto-option fee estimate.

    The public fee schedule caps transaction fees at 10% of option premium.
    The 0.03% underlying-value branch is retained, with the premium cap acting
    as the fail-safe when index data is unavailable.
    """

    price = max(Decimal("0"), _d(option_price))
    index = max(Decimal("0"), _d(index_price))
    qty = max(Decimal("0"), _d(quantity))
    unit_d = max(Decimal("0"), _d(unit, "1"))
    premium_cap = price * Decimal("0.10")
    per_unit = min(index * Decimal("0.0003"), premium_cap) if index > 0 else premium_cap
    return float(max(Decimal("0"), per_unit * qty * unit_d))


def build_long_option_entry_plan(
    *,
    ask_price,
    index_price,
    unit,
    min_qty,
    step_size,
    cash_bankroll_usdt,
    entry_fraction=1.00,
    capital_limit_usdt=OPTIONS_CAPITAL_LIMIT_USDT,
):
    ask = _d(ask_price)
    unit_d = _d(unit, "1")
    min_qty_d = _d(min_qty)
    step_d = _d(step_size)
    bankroll = max(Decimal("0"), _d(cash_bankroll_usdt))
    hard_cap = min(
        Decimal(str(OPTIONS_CAPITAL_LIMIT_USDT)),
        max(Decimal("0"), _d(capital_limit_usdt)),
        bankroll,
    )
    fraction = min(Decimal("1.00"), max(Decimal("0.10"), _d(entry_fraction, "1.00")))
    spend_cap = hard_cap * fraction
    if ask <= 0 or unit_d <= 0 or min_qty_d <= 0 or step_d <= 0 or spend_cap <= 0:
        return {"accepted": False, "reason": "INVALID_OPTION_ORDER_INPUT"}

    # Iterate once after fee estimation so premium + entry fee remains below
    # both the configured fraction and the absolute $20 ceiling.
    qty = floor_to_step(spend_cap / (ask * unit_d), step_d)
    if qty < min_qty_d:
        return {
            "accepted": False,
            "reason": "MINIMUM_OPTION_CONTRACT_EXCEEDS_AVAILABLE_BANKROLL",
            "spend_cap_usdt": float(spend_cap),
        }
    for _ in range(3):
        premium = ask * qty * unit_d
        fee = Decimal(
            str(estimate_option_fee(ask, index_price, qty, unit=unit_d))
        )
        total = premium + fee
        if total <= spend_cap and total <= hard_cap:
            break
        qty = floor_to_step((spend_cap - fee) / (ask * unit_d), step_d)
    premium = ask * qty * unit_d
    fee = Decimal(str(estimate_option_fee(ask, index_price, qty, unit=unit_d)))
    total = premium + fee
    if qty < min_qty_d or total <= 0 or total > spend_cap or total > hard_cap:
        return {
            "accepted": False,
            "reason": "OPTION_PREMIUM_PLUS_FEE_EXCEEDS_HARD_CAP",
            "spend_cap_usdt": float(spend_cap),
        }
    return {
        "accepted": True,
        "quantity": format(qty.normalize(), "f"),
        "premium_usdt": float(premium),
        "estimated_entry_fee_usdt": float(fee),
        "total_entry_cost_usdt": float(total),
        "spend_cap_usdt": float(spend_cap),
        "hard_cap_usdt": float(hard_cap),
    }


__all__ = (
    "build_long_option_entry_plan",
    "estimate_option_fee",
    "floor_to_step",
)

"""Liquidation-aware stop-loss validation for crypto futures positions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping


ZERO = Decimal("0")


def as_decimal(value: Any, *, name: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"invalid {name}: {value!r}")
    return result


@dataclass(frozen=True)
class LiquidationSafetyConfig:
    minimum_buffer_pct: Decimal = Decimal("0.02")
    minimum_buffer_ticks: int = 20
    stop_working_type: str = "MARK_PRICE"
    stop_price_protect: bool = False
    auto_reduce_leverage: bool = False


@dataclass(frozen=True)
class LiquidationSafetyResult:
    valid: bool
    reason: str
    side: str
    entry_price: Decimal
    stop_price: Decimal
    liquidation_price: Decimal
    buffer_abs: Decimal
    buffer_pct: Decimal
    minimum_safe_stop: Decimal | None
    maximum_safe_stop: Decimal | None
    tick_size: Decimal
    working_type: str


def resolve_liquidation_safety_config(
    values: Mapping[str, Any] | None = None,
) -> LiquidationSafetyConfig:
    """Resolve the single canonical source of liquidation safety defaults."""

    values = values or {}
    pct = as_decimal(
        values.get("minimum_liquidation_buffer_pct", "0.02"),
        name="minimum_liquidation_buffer_pct",
    )
    ticks = int(values.get("minimum_liquidation_buffer_ticks", 20) or 20)
    if pct < ZERO or ticks < 0:
        raise ValueError("liquidation safety buffers cannot be negative")
    return LiquidationSafetyConfig(
        minimum_buffer_pct=pct,
        minimum_buffer_ticks=ticks,
        stop_working_type=str(values.get("stop_working_type", "MARK_PRICE") or "MARK_PRICE").upper(),
        stop_price_protect=bool(values.get("stop_price_protect", False)),
        auto_reduce_leverage=bool(
            values.get("auto_reduce_leverage_for_liquidation_safety", False)
        ),
    )


def validate_stop_against_liquidation(
    side: str,
    stop_price: Any,
    liquidation_price: Any,
    tick_size: Any,
    minimum_buffer_pct: Any,
    minimum_buffer_ticks: int,
    working_type: str,
    entry_price: Any = 0,
    *,
    accepted_working_types: set[str] | frozenset[str] | tuple[str, ...] | None = None,
    non_mark_minimum_buffer_pct: Any | None = None,
    non_mark_buffer_multiplier: Any = 1,
) -> LiquidationSafetyResult:
    side_value = str(side or "").strip().lower()
    stop = as_decimal(stop_price, name="stop_price")
    liquidation = as_decimal(liquidation_price, name="liquidation_price")
    tick = as_decimal(tick_size, name="tick_size")
    entry = as_decimal(entry_price, name="entry_price")
    buffer_rate = as_decimal(minimum_buffer_pct, name="minimum_buffer_pct")
    working = str(working_type or "").strip().upper()
    accepted = {
        str(value or "").strip().upper()
        for value in (accepted_working_types or {"MARK_PRICE"})
        if str(value or "").strip()
    }
    if not accepted:
        accepted = {"MARK_PRICE"}
    effective_buffer_rate = buffer_rate
    if working != "MARK_PRICE" and working in accepted:
        multiplier = as_decimal(non_mark_buffer_multiplier, name="non_mark_buffer_multiplier")
        if multiplier < Decimal("1"):
            multiplier = Decimal("1")
        effective_buffer_rate = buffer_rate * multiplier
        if non_mark_minimum_buffer_pct is not None:
            effective_buffer_rate = max(
                effective_buffer_rate,
                as_decimal(
                    non_mark_minimum_buffer_pct,
                    name="non_mark_minimum_buffer_pct",
                ),
            )

    if side_value not in {"long", "short"}:
        raise ValueError(f"invalid side: {side!r}")
    if stop <= ZERO or liquidation <= ZERO or tick <= ZERO or buffer_rate < ZERO:
        return LiquidationSafetyResult(
            False,
            "LIQUIDATION_SAFETY_UNKNOWN",
            side_value,
            entry,
            stop,
            liquidation,
            ZERO,
            ZERO,
            None,
            None,
            tick,
            working,
        )

    buffer_abs = max(
        liquidation * effective_buffer_rate,
        tick * max(0, int(minimum_buffer_ticks or 0)),
    )
    buffer_pct = buffer_abs / liquidation
    long_minimum = liquidation + buffer_abs
    short_maximum = liquidation - buffer_abs
    minimum_safe_stop = long_minimum if side_value == "long" else None
    maximum_safe_stop = short_maximum if side_value == "short" else None

    if working not in accepted:
        reason = (
            "STOP_WORKING_TYPE_NOT_MARK_PRICE"
            if accepted == {"MARK_PRICE"}
            else "STOP_WORKING_TYPE_NOT_ACCEPTED"
        )
        valid = False
    elif side_value == "long" and stop < liquidation:
        reason = "LONG_STOP_BELOW_LIQUIDATION"
        valid = False
    elif side_value == "long" and stop == liquidation:
        reason = "LONG_STOP_AT_LIQUIDATION"
        valid = False
    elif side_value == "long" and stop <= long_minimum:
        reason = "LONG_STOP_LIQUIDATION_BUFFER_INSUFFICIENT"
        valid = False
    elif side_value == "short" and stop > liquidation:
        reason = "SHORT_STOP_ABOVE_LIQUIDATION"
        valid = False
    elif side_value == "short" and stop == liquidation:
        reason = "SHORT_STOP_AT_LIQUIDATION"
        valid = False
    elif side_value == "short" and stop >= short_maximum:
        reason = "SHORT_STOP_LIQUIDATION_BUFFER_INSUFFICIENT"
        valid = False
    else:
        reason = "SAFE" if working == "MARK_PRICE" else f"SAFE_EXTERNAL_{working}"
        valid = True

    return LiquidationSafetyResult(
        valid,
        reason,
        side_value,
        entry,
        stop,
        liquidation,
        buffer_abs,
        buffer_pct,
        minimum_safe_stop,
        maximum_safe_stop,
        tick,
        working,
    )


def estimate_isolated_liquidation_price(
    side: str,
    entry_price: Any,
    leverage: int,
    maintenance_margin_rate: Any,
) -> Decimal:
    """Conservative pre-entry estimate for a fresh isolated USD-M position."""

    entry = as_decimal(entry_price, name="entry_price")
    mmr = as_decimal(maintenance_margin_rate, name="maintenance_margin_rate")
    leverage_value = int(leverage)
    if entry <= ZERO or leverage_value <= 0 or mmr < ZERO or mmr >= Decimal("1"):
        raise ValueError("invalid liquidation estimate inputs")
    inverse_leverage = Decimal("1") / Decimal(leverage_value)
    if str(side or "").lower() == "long":
        estimate = entry * (Decimal("1") - inverse_leverage + mmr)
    elif str(side or "").lower() == "short":
        estimate = entry * (Decimal("1") + inverse_leverage - mmr)
    else:
        raise ValueError(f"invalid side: {side!r}")
    if estimate <= ZERO:
        raise ValueError("estimated liquidation price is not positive")
    return estimate


def quantize_price_for_safety(value: Any, tick_size: Any, *, direction: str) -> Decimal:
    price = as_decimal(value, name="price")
    tick = as_decimal(tick_size, name="tick_size")
    if tick <= ZERO:
        raise ValueError("tick_size must be positive")
    units = price / tick
    if direction == "up":
        units = units.to_integral_value(rounding=ROUND_CEILING)
    elif direction == "down":
        units = units.to_integral_value(rounding=ROUND_FLOOR)
    else:
        raise ValueError("direction must be 'up' or 'down'")
    return units * tick

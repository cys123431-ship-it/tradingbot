from decimal import Decimal

import pytest

from trading_safety.liquidation_guard import (
    estimate_isolated_liquidation_price,
    quantize_price_for_safety,
    validate_stop_against_liquidation,
)


def validate(side, stop, liquidation, *, tick="0.0000001", pct="0.02", ticks=20, working="MARK_PRICE"):
    return validate_stop_against_liquidation(
        side,
        stop,
        liquidation,
        tick,
        pct,
        ticks,
        working,
        entry_price="0.0002339" if Decimal(str(liquidation)) < 1 else "110",
    )


def test_hmstr_long_stop_below_liquidation_is_rejected():
    result = validate("long", "0.0001885", "0.0001891")
    assert result.valid is False
    assert result.reason == "LONG_STOP_BELOW_LIQUIDATION"


@pytest.mark.parametrize(
    ("side", "stop", "liquidation", "expected_reason"),
    [
        ("long", "100", "100", "LONG_STOP_AT_LIQUIDATION"),
        ("long", "101", "100", "LONG_STOP_LIQUIDATION_BUFFER_INSUFFICIENT"),
        ("short", "125", "120", "SHORT_STOP_ABOVE_LIQUIDATION"),
        ("short", "119", "120", "SHORT_STOP_LIQUIDATION_BUFFER_INSUFFICIENT"),
    ],
)
def test_liquidation_boundary_rejections(side, stop, liquidation, expected_reason):
    result = validate(side, stop, liquidation, tick="0.1", ticks=0)
    assert result.valid is False
    assert result.reason == expected_reason


@pytest.mark.parametrize(
    ("side", "stop", "liquidation"),
    [("long", "105", "100"), ("short", "115", "120")],
)
def test_liquidation_safe_stops_are_allowed(side, stop, liquidation):
    assert validate(side, stop, liquidation, tick="0.1", ticks=0).valid is True


def test_contract_price_stop_is_not_protected():
    result = validate("long", "105", "100", tick="0.1", ticks=0, working="CONTRACT_PRICE")
    assert result.valid is False
    assert result.reason == "STOP_WORKING_TYPE_NOT_MARK_PRICE"


def test_mark_price_stop_passes_trigger_basis_check():
    assert validate("long", "105", "100", tick="0.1", ticks=0).valid is True


def test_tick_rounding_is_revalidated_after_rounding():
    raw = Decimal("102.1")
    rounded = quantize_price_for_safety(raw, "1", direction="down")
    assert validate("long", raw, "100", tick="1", ticks=0).valid is True
    result = validate("long", rounded, "100", tick="1", ticks=0)
    assert result.valid is False
    assert result.reason == "LONG_STOP_LIQUIDATION_BUFFER_INSUFFICIENT"


def test_isolated_liquidation_estimate_moves_away_when_leverage_is_reduced():
    five_x = estimate_isolated_liquidation_price("long", "100", 5, "0.005")
    three_x = estimate_isolated_liquidation_price("long", "100", 3, "0.005")
    assert three_x < five_x


def test_partial_exit_liquidation_change_requires_revalidation():
    assert validate("long", "105", "100", tick="0.1", ticks=0).valid is True
    changed = validate("long", "105", "104", tick="0.1", ticks=0)
    assert changed.valid is False
    assert changed.reason == "LONG_STOP_LIQUIDATION_BUFFER_INSUFFICIENT"


def test_unsafe_replacement_stop_never_becomes_protected():
    safe = validate("short", "115", "120", tick="0.1", ticks=0)
    replacement = validate("short", "119", "120", tick="0.1", ticks=0)
    assert safe.valid is True
    assert replacement.valid is False

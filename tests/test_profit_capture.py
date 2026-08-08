import pytest

from utbreakout.profit_capture import (
    QUAD_CONFIRMATION_RISK_MULTIPLIERS,
    bounded_structure_anchor,
    exit_bars_for_signal_holding_period,
    weakest_link_risk_multiplier,
)


def test_quality_budgets_use_weakest_link_instead_of_compounding():
    result = weakest_link_risk_multiplier((0.80, 0.75, 0.90, 0.85))

    assert result == pytest.approx(0.75)
    assert result > 0.80 * 0.75 * 0.90 * 0.85


def test_four_hour_holding_period_is_converted_to_fifteen_minute_exit_bars():
    assert exit_bars_for_signal_holding_period(
        8,
        signal_timeframe="4h",
        exit_timeframe="15m",
    ) == 128


def test_distant_structure_anchor_does_not_expand_trend_stop_and_shrink_size():
    usable, distance = bounded_structure_anchor(
        entry_price=100,
        atr_value=2,
        structure_stop=90,
        max_distance_atr=1.6,
    )

    assert usable is None
    assert distance == pytest.approx(5.0)


def test_single_signal_remains_reduced_but_meaningful():
    assert QUAD_CONFIRMATION_RISK_MULTIPLIERS == {
        1: 0.65,
        2: 0.85,
        3: 0.95,
        4: 1.0,
        5: 1.0,
    }

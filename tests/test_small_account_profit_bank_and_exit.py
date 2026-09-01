import pytest

from utbreakout.small_account.exit import evaluate_progress_failure_exit
from utbreakout.small_account.risk import resolve_profit_bank_risk_budget


def test_profit_bank_waits_below_activation_and_never_halts_trading():
    decision = resolve_profit_bank_risk_budget(
        account_equity=100.0,
        daily_realized_pnl_usdt=3.0,
        normal_full_risk_usdt=8.0,
        initial_fraction=0.65,
    )

    assert decision.active is False
    assert decision.risk_scale == pytest.approx(1.0)
    assert decision.effective_initial_risk_usdt == pytest.approx(5.2)


def test_profit_bank_protects_half_of_positive_day_with_fifty_percent_floor():
    decision = resolve_profit_bank_risk_budget(
        account_equity=270.0,
        daily_realized_pnl_usdt=13.4041,
        normal_full_risk_usdt=21.6,
        initial_fraction=0.65,
        activation_multiple=0.75,
        protect_fraction=0.50,
        minimum_risk_scale=0.50,
    )

    assert decision.active is True
    assert decision.activation_profit_usdt == pytest.approx(10.53)
    assert decision.protected_profit_usdt == pytest.approx(6.70205)
    assert decision.risk_scale == pytest.approx(0.50)
    assert decision.effective_initial_risk_usdt == pytest.approx(7.02)


def test_profit_bank_does_not_reduce_after_a_losing_day():
    decision = resolve_profit_bank_risk_budget(
        account_equity=100.0,
        daily_realized_pnl_usdt=-34.5,
        normal_full_risk_usdt=8.0,
        initial_fraction=0.65,
    )

    assert decision.active is False
    assert decision.risk_scale == pytest.approx(1.0)


def test_progress_failure_requires_mark_reversal_and_two_closed_bar_checks():
    decision = evaluate_progress_failure_exit(
        enabled=True,
        small_account_active=True,
        tp1_filled=False,
        bars_held=3,
        mark_mfe_r=0.22,
        mark_current_r=-0.14,
        fast_support_lost=True,
        consecutive_entry_closes_lost=True,
        impulse_lost=False,
    )

    assert decision.should_exit is True
    assert decision.confirmation_count == 2


def test_progress_failure_ignores_contract_wick_without_mark_progress():
    decision = evaluate_progress_failure_exit(
        enabled=True,
        small_account_active=True,
        tp1_filled=False,
        bars_held=3,
        mark_mfe_r=0.05,
        mark_current_r=-0.20,
        fast_support_lost=True,
        consecutive_entry_closes_lost=True,
        impulse_lost=True,
    )

    assert decision.should_exit is False
    assert "insufficient mark progress" in decision.reason


def test_progress_failure_never_applies_to_pre_upgrade_or_partial_position():
    disabled = evaluate_progress_failure_exit(
        enabled=False,
        small_account_active=True,
        tp1_filled=False,
        bars_held=3,
        mark_mfe_r=0.30,
        mark_current_r=-0.20,
        fast_support_lost=True,
        consecutive_entry_closes_lost=True,
        impulse_lost=True,
    )
    partial = evaluate_progress_failure_exit(
        enabled=True,
        small_account_active=True,
        tp1_filled=True,
        bars_held=3,
        mark_mfe_r=0.30,
        mark_current_r=-0.20,
        fast_support_lost=True,
        consecutive_entry_closes_lost=True,
        impulse_lost=True,
    )

    assert disabled.should_exit is False
    assert partial.should_exit is False

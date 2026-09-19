from bot_runtime.ema200_utbot_rsi import (
    EMA200_UTBOT_RSI_STRATEGY,
    build_ema200_utbot_rsi_risk_plan,
    evaluate_ema200_utbot_rsi_entry,
    evaluate_ema200_utbot_rsi_loss_gate,
    normalize_ema200_utbot_rsi_config,
)
from bot_runtime.strategy_registry import CORE_STRATEGIES


def test_strategy_registered_in_core_strategies():
    assert EMA200_UTBOT_RSI_STRATEGY in CORE_STRATEGIES


def test_long_requires_ordered_ut_then_fresh_rsi_cross_above_ema200():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal == "long"
    assert detail["ut_precedes_rsi"] is True
    assert detail["rsi_cross_up"] is True


def test_long_rejects_rsi_cross_that_happened_before_ut_buy():
    signal, reason, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=3_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None
    assert detail["ut_precedes_rsi"] is False
    assert "먼저 확정되지 않음" in reason


def test_long_requires_fresh_cross_not_merely_rsi_above_50():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=55.0,
        curr_rsi=57.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None
    assert detail["rsi_cross_up"] is False


def test_short_is_exact_inverse():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=90.0,
        ema200=100.0,
        ut_state="short",
        ut_last_signal_side="short",
        ut_last_signal_ts=1_000,
        prev_rsi=51.0,
        curr_rsi=49.0,
        rsi_signal_ts=2_000,
    )
    assert signal == "short"
    assert detail["ema_short_ok"] is True
    assert detail["rsi_cross_down"] is True


def test_wrong_ema_side_blocks_signal():
    signal, _, _ = evaluate_ema200_utbot_rsi_entry(
        close_price=99.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None


def test_risk_plan_sizes_from_loss_budget_and_emergency_distance():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=1000.0,
        free_balance=1000.0,
        entry_price=100.0,
        config={
            "risk_per_trade_percent": 0.5,
            "emergency_exit_percent": 5.0,
            "leverage": 5,
        },
    )
    assert plan["risk_budget_usdt"] == 5.0
    assert plan["planned_notional"] == 100.0
    assert plan["planned_margin"] == 20.0
    assert plan["planned_emergency_loss_usdt"] == 5.0
    assert plan["planned_qty"] == 1.0


def test_risk_plan_caps_position_to_available_margin_without_increasing_risk():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=1000.0,
        free_balance=5.0,
        entry_price=100.0,
        config={
            "risk_per_trade_percent": 0.5,
            "emergency_exit_percent": 5.0,
            "leverage": 5,
        },
    )
    assert plan["margin_cap_applied"] is True
    assert plan["planned_notional"] < 100.0
    assert plan["planned_emergency_loss_usdt"] < plan["risk_budget_usdt"]


def test_daily_and_weekly_limits_block_new_entries_only_via_gate_result():
    gate = evaluate_ema200_utbot_rsi_loss_gate(
        account_equity=1000.0,
        daily_realized_pnl=-21.0,
        weekly_realized_pnl=-21.0,
        config={
            "daily_loss_limit_percent": 2.0,
            "weekly_loss_limit_percent": 5.0,
        },
    )
    assert gate["allowed"] is False
    assert gate["daily_blocked"] is True
    assert gate["weekly_blocked"] is False


def test_weekly_limit_is_never_normalized_below_daily_limit():
    cfg = normalize_ema200_utbot_rsi_config(
        {
            "daily_loss_limit_percent": 6.0,
            "weekly_loss_limit_percent": 4.0,
        }
    )
    assert cfg["weekly_loss_limit_percent"] >= cfg["daily_loss_limit_percent"]

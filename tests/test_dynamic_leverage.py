import pytest

from utbreakout.dynamic_leverage import (
    apply_dynamic_leverage_to_plan,
    normalize_dynamic_leverage_config,
    resolve_small_account_full_margin,
    select_dynamic_leverage,
)


def _plan(**overrides):
    plan = {
        "side": "long",
        "entry_price": 100.0,
        "risk_distance": 1.0,
        "risk_distance_pct": 1.0,
        "atr": 1.0,
        "atr_pct": 1.0,
        "qty": 10.0,
        "planned_notional": 1_000.0,
        "planned_margin": 200.0,
        "risk_usdt": 10.0,
        "rr_multiple": 3.0,
        "vmt_score": 75.0,
        "l2_state": "deep_balanced",
        "l2_risk_multiplier": 1.0,
        "l2_gate": {
            "allowed": True,
            "state": "deep_balanced",
            "risk_multiplier": 1.0,
        },
        "ev_time_stop_enabled": True,
        "ev_time_stop_bars": 96,
        "ev_time_stop_min_mfe_r": 0.45,
        "atr_trailing_activation_r": 1.60,
    }
    plan.update(overrides)
    return plan


def test_dynamic_leverage_moves_both_below_and_above_old_five_x():
    ordinary = select_dynamic_leverage(_plan())
    strong_single = select_dynamic_leverage(_plan(vmt_score=92.0))
    confirmed = select_dynamic_leverage(
        _plan(quad_alpha_score=84.0, quad_alpha_confirmation_count=2)
    )
    high_conviction = select_dynamic_leverage(
        _plan(quad_alpha_score=90.0, quad_alpha_confirmation_count=3)
    )
    volatile = select_dynamic_leverage(
        _plan(
            quad_alpha_score=90.0,
            quad_alpha_confirmation_count=3,
            atr_pct=5.0,
        )
    )

    assert ordinary.leverage == 5
    assert strong_single.leverage == 8
    assert confirmed.leverage == 8
    assert high_conviction.leverage == 10
    assert volatile.leverage == 4


def test_strategy_sizing_overlay_does_not_veto_leverage_quality():
    decision = select_dynamic_leverage(
        _plan(
            quad_alpha_score=92.0,
            quad_alpha_confirmation_count=1,
            vmt_risk_multiplier=0.25,
        )
    )

    assert decision.leverage == 8
    assert decision.tier == "strong_single"
    assert decision.risk_quality_multiplier == 1.0


def test_performance_allocator_still_blocks_aggressive_leverage():
    decision = select_dynamic_leverage(
        _plan(
            quad_alpha_score=92.0,
            quad_alpha_confirmation_count=1,
            vmt_risk_multiplier=0.60,
            strategy_allocator_multiplier=0.50,
        )
    )

    assert decision.leverage == 3
    assert decision.tier == "defensive_quality"


def test_stressed_order_book_forces_defensive_minimum():
    decision = select_dynamic_leverage(
        _plan(
            quad_alpha_score=95.0,
            quad_alpha_confirmation_count=5,
            l2_state="stressed_thin",
            l2_risk_multiplier=0.0,
            l2_gate={
                "allowed": False,
                "state": "stressed_thin",
                "risk_multiplier": 0.0,
            },
        )
    )

    assert decision.leverage == 2
    assert decision.tier == "defensive_l2"


def test_high_leverage_shortens_time_stop_and_arms_trailing_earlier():
    updated = apply_dynamic_leverage_to_plan(
        _plan(quad_alpha_score=90.0, quad_alpha_confirmation_count=3)
    )

    assert updated["leverage"] == 10
    assert updated["ev_time_stop_bars"] == 8
    assert updated["ev_time_stop_min_mfe_r"] == 0.70
    assert updated["atr_trailing_activation_r"] == 1.00
    assert updated["dynamic_leverage_monitor_timeframe"] == "15m"


def test_reassessment_restores_original_exit_profile_before_lower_tier():
    high = apply_dynamic_leverage_to_plan(
        _plan(quad_alpha_score=90.0, quad_alpha_confirmation_count=3)
    )
    lower = dict(high)
    lower.update(
        {
            "quad_alpha_score": 72.0,
            "quad_alpha_confirmation_count": 1,
            "vmt_score": 72.0,
        }
    )
    reassessed = apply_dynamic_leverage_to_plan(lower)

    assert reassessed["leverage"] == 5
    assert reassessed["ev_time_stop_bars"] == 96
    assert reassessed["ev_time_stop_min_mfe_r"] == 0.45
    assert reassessed["atr_trailing_activation_r"] == 1.60
    assert "dynamic_leverage_monitor_timeframe" not in reassessed


def test_dynamic_leverage_restores_only_original_stop_budget_plan():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            quad_alpha_score=90.0,
            quad_alpha_confirmation_count=3,
            qty=5.0,
            planned_notional=500.0,
            planned_margin=100.0,
            risk_usdt=5.0,
            position_cap_applied=True,
            position_cap_original_notional=1_000.0,
            position_cap_original_risk_usdt=10.0,
        ),
        free_balance=200.0,
    )

    assert updated["leverage"] == 10
    assert updated["planned_notional"] == 1_000.0
    assert updated["qty"] == 10.0
    assert updated["risk_usdt"] == 10.0
    assert updated["dynamic_leverage_restored_notional"] == 500.0
    assert updated["position_cap_applied"] is False


def test_exchange_margin_cap_still_limits_high_leverage_plan():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            quad_alpha_score=90.0,
            quad_alpha_confirmation_count=3,
            position_cap_original_notional=1_800.0,
        ),
        free_balance=150.0,
    )

    assert updated["leverage"] == 10
    assert updated["planned_notional"] == 1_470.0
    assert updated["position_cap_applied"] is True
    assert updated["risk_usdt"] == 14.70


def test_small_account_uses_all_available_margin_and_at_least_five_x():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            atr_pct=6.0,
            qty=1.0,
            planned_notional=100.0,
            planned_margin=50.0,
            risk_usdt=1.0,
        ),
        free_balance=800.0,
        account_equity=900.0,
    )

    assert updated["small_account_full_margin_applied"] is True
    assert updated["leverage"] >= 5
    assert updated["planned_margin"] == 800.0
    assert updated["planned_notional"] == pytest.approx(
        800.0 * updated["leverage"]
    )
    assert updated["qty"] == pytest.approx(
        updated["planned_notional"] / updated["entry_price"]
    )


def test_small_account_threshold_is_inclusive_but_larger_accounts_are_unchanged():
    at_threshold = resolve_small_account_full_margin(
        account_equity=1_000.0,
        free_balance=990.0,
    )
    above_threshold = apply_dynamic_leverage_to_plan(
        _plan(atr_pct=6.0),
        free_balance=990.0,
        account_equity=1_000.01,
    )

    assert at_threshold["active"] is True
    assert at_threshold["minimum_leverage"] == 5
    assert above_threshold["small_account_full_margin_applied"] is False
    assert above_threshold["leverage"] == 4
    assert above_threshold["planned_notional"] == 1_000.0


def test_small_account_policy_can_be_disabled_without_changing_operator_maximum():
    cfg = normalize_dynamic_leverage_config(
        {
            "small_account_full_margin_enabled": False,
            "small_account_min_leverage": 5,
            "max_leverage": 3,
        }
    )
    updated = apply_dynamic_leverage_to_plan(
        _plan(atr_pct=6.0),
        cfg,
        free_balance=100.0,
        account_equity=100.0,
    )

    assert cfg["max_leverage"] == 3
    assert updated["small_account_full_margin_applied"] is False
    assert updated["leverage"] <= 3


def test_operator_maximum_caps_opportunity_without_making_it_fixed():
    cfg = normalize_dynamic_leverage_config({"max_leverage": 7})
    high = select_dynamic_leverage(
        _plan(quad_alpha_score=95.0, quad_alpha_confirmation_count=5),
        cfg,
    )
    defensive = select_dynamic_leverage(
        _plan(atr_pct=6.0),
        cfg,
    )

    assert high.leverage == 7
    assert defensive.leverage < high.leverage


def test_strategy_ceiling_caps_adaptive_trend_above_small_account_threshold():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=99.0,
            strategy_leverage_ceiling=10,
        ),
        free_balance=2_000.0,
        account_equity=2_000.0,
    )

    assert updated["small_account_aggressive_active"] is False
    assert updated["leverage"] == 10
    assert updated["dynamic_leverage_tier"].endswith("+strategy_cap")


def test_persisted_v1_dynamic_config_migrates_adaptive_trend_to_fifteen_x_ceiling():
    cfg = normalize_dynamic_leverage_config(
        {"max_leverage": 10, "adaptive_trend_elite_leverage": 10}
    )

    assert cfg["max_leverage"] == 10
    assert cfg["adaptive_trend_profile_version"] == "adaptive_trend_leverage_v2"
    assert cfg["adaptive_trend_elite_leverage"] == 15
    assert cfg["adaptive_trend_max_leverage"] == 15


def test_disabled_mode_preserves_configured_plan_leverage_and_exit_profile():
    updated = apply_dynamic_leverage_to_plan(
        _plan(leverage=5, ev_time_stop_bars=96, atr_trailing_activation_r=1.6),
        {"enabled": False},
    )

    assert updated["leverage"] == 5
    assert updated["ev_time_stop_bars"] == 96
    assert updated["atr_trailing_activation_r"] == 1.6


def test_adaptive_trend_uses_fifteen_x_only_for_elite_tight_stop_setup():
    decision = select_dynamic_leverage(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=96.0,
            atr_pct=1.4,
            risk_distance_pct=2.5,
            market_quality_risk_multiplier=0.90,
            l2_risk_multiplier=0.90,
            l2_gate={
                "allowed": True,
                "state": "deep_balanced",
                "risk_multiplier": 0.90,
            },
        )
    )

    assert decision.leverage == 15
    assert decision.tier == "adaptive_trend_elite"
    assert decision.stop_distance_pct == 2.5


def test_adaptive_trend_uses_ten_x_for_strong_normal_volatility_setup():
    decision = select_dynamic_leverage(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=92.0,
            atr_pct=2.1,
            risk_distance_pct=4.0,
            market_quality_risk_multiplier=0.75,
            l2_risk_multiplier=0.80,
            l2_gate={
                "allowed": True,
                "state": "deep_balanced",
                "risk_multiplier": 0.80,
            },
        )
    )

    assert decision.leverage == 10
    assert decision.tier == "adaptive_trend_strong"


def test_adaptive_trend_stop_buffer_limits_healthy_opportunity_setup():
    decision = select_dynamic_leverage(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=86.0,
            atr_pct=3.0,
            risk_distance_pct=5.5,
            market_quality_risk_multiplier=0.65,
            l2_risk_multiplier=0.70,
            l2_gate={
                "allowed": True,
                "state": "deep_balanced",
                "risk_multiplier": 0.70,
            },
        )
    )

    assert decision.leverage == 7
    assert decision.tier == "adaptive_trend_opportunity"


def test_adaptive_trend_keeps_cys_like_wide_stop_at_two_x():
    decision = select_dynamic_leverage(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=95.4,
            atr_pct=6.060762544108787,
            risk_distance_pct=12.121525088217574,
            market_quality_risk_multiplier=0.50,
            l2_risk_multiplier=0.50,
            l2_gate={
                "allowed": True,
                "state": "deep_balanced",
                "risk_multiplier": 0.50,
            },
        )
    )

    assert decision.leverage == 2
    assert decision.tier == "adaptive_trend_extreme_volatility"
    assert decision.risk_quality_multiplier == 0.50


def test_adaptive_trend_high_leverage_preserves_long_horizon_exit_profile():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=96.0,
            atr_pct=1.4,
            risk_distance_pct=2.5,
            market_quality_risk_multiplier=0.90,
            l2_risk_multiplier=0.90,
            l2_gate={
                "allowed": True,
                "state": "deep_balanced",
                "risk_multiplier": 0.90,
            },
        )
    )

    assert updated["leverage"] == 15
    assert updated["ev_time_stop_bars"] == 96
    assert updated["ev_time_stop_min_mfe_r"] == 0.45
    assert updated["atr_trailing_activation_r"] == 1.60
    assert updated["dynamic_leverage_monitor_timeframe"] == "15m"


def test_adaptive_trend_dynamic_leverage_does_not_expand_staged_initial_order():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=96.0,
            adaptive_trend_target_notional=2_000.0,
            position_cap_original_notional=1_000.0,
        ),
        free_balance=1_000.0,
        account_equity=5_000.0,
    )

    assert updated["leverage"] == 15
    assert updated["planned_notional"] == pytest.approx(1_000.0)
    assert updated["dynamic_leverage_restored_notional"] == pytest.approx(0.0)


def test_adaptive_trend_small_account_uses_95_percent_budget_and_65_percent_initial_stage():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=70.0,
            planned_notional=300.0,
            qty=3.0,
        ),
        free_balance=800.0,
        account_equity=800.0,
    )

    assert updated["leverage"] >= 4
    assert updated["leverage"] == 4
    assert updated["planned_margin"] == pytest.approx(800.0 * 0.95 * 0.65)
    assert updated["planned_notional"] == pytest.approx(800.0 * 0.95 * 0.65 * 4)
    assert updated["adaptive_trend_target_notional"] == pytest.approx(800.0 * 0.95 * 4)
    assert updated["small_account_full_margin_applied"] is False
    assert updated["small_account_aggressive_active"] is True
    assert updated["small_account_aggressive_blocked"] is False
    assert updated["risk_budget_mode"] == "adaptive_trend_small_account_aggressive"


def test_adaptive_trend_small_account_applies_evidence_margin_once_at_final_sizing():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=96.0,
            adaptive_trend_risk_tier="elite",
            adaptive_breakout_trend_metrics={"risk_tier": "elite"},
            small_account_initial_margin_fraction=0.8125,
            risk_distance=2.0,
            risk_distance_pct=2.0,
            planned_notional=100.0,
            qty=1.0,
        ),
        free_balance=100.0,
        account_equity=100.0,
    )

    expected_margin = 100.0 * 0.95 * 0.8125
    assert updated["leverage"] == 7
    assert updated["planned_margin"] == pytest.approx(expected_margin)
    assert updated["planned_notional"] == pytest.approx(expected_margin * 7)
    assert updated["small_account_margin_utilization"] == pytest.approx(0.8125)


def test_adaptive_trend_small_account_preserves_four_x_floor_across_two_phase_sizing():
    plan = _plan(
        strategy="adaptive_breakout_trend_v1",
        adaptive_breakout_trend_score=70.0,
        adaptive_trend_risk_tier="base",
        adaptive_breakout_trend_metrics={"risk_tier": "base"},
        risk_distance=2.0,
        risk_distance_pct=2.0,
        planned_notional=100.0,
        qty=1.0,
        small_account_min_leverage=4,
        small_account_strong_leverage=6,
        small_account_elite_leverage=7,
        small_account_leverage_steps=(4, 5, 6, 7),
    )

    staged = apply_dynamic_leverage_to_plan(plan)
    sized = apply_dynamic_leverage_to_plan(
        staged,
        free_balance=100.0,
        account_equity=100.0,
    )

    assert staged["small_account_min_leverage"] == 4
    assert sized["small_account_min_leverage"] == 4
    assert sized["leverage"] == 4


@pytest.mark.parametrize(
    ("risk_tier", "score", "stop_pct", "expected_leverage", "loss_cap"),
    (
        ("base", 70.0, 6.0, 4, 8.0),
        ("strong", 82.0, 3.5, 6, 12.0),
        ("elite", 96.0, 3.5, 7, 16.0),
    ),
)
def test_adaptive_trend_small_account_selects_highest_allowed_tier_leverage(
    risk_tier,
    score,
    stop_pct,
    expected_leverage,
    loss_cap,
):
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=score,
            adaptive_trend_risk_tier=risk_tier,
            adaptive_breakout_trend_metrics={"risk_tier": risk_tier},
            risk_distance=stop_pct,
            risk_distance_pct=stop_pct,
            planned_notional=100.0,
            qty=1.0,
            small_account_aggressive_daily_pnl_usdt=0.0,
        ),
        free_balance=100.0,
        account_equity=100.0,
    )

    full_loss_rate = (stop_pct + 0.20) / 100.0
    expected_full_notional = min(
        100.0 * 0.95 * expected_leverage,
        loss_cap / full_loss_rate,
    )
    expected_initial_notional = expected_full_notional * 0.65
    assert updated["leverage"] == expected_leverage
    assert updated["planned_notional"] == pytest.approx(expected_initial_notional)
    assert updated["planned_margin"] == pytest.approx(
        expected_initial_notional / expected_leverage
    )
    assert updated["adaptive_trend_target_notional"] == pytest.approx(
        expected_full_notional
    )
    assert updated["small_account_aggressive_max_loss_percent"] == pytest.approx(loss_cap)
    assert updated["small_account_aggressive_full_target_loss_usdt"] <= loss_cap + 1e-9
    assert updated["small_account_aggressive_full_target_loss_percent"] <= loss_cap + 1e-9
    assert updated["small_account_aggressive_projected_loss_usdt"] <= loss_cap * 0.65 + 1e-9
    assert updated["small_account_stop_distance_risk_sizing_applied"] is True


def test_adaptive_trend_small_account_wide_stop_shrinks_quantity_instead_of_blocking():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=70.0,
            adaptive_trend_risk_tier="base",
            adaptive_breakout_trend_metrics={"risk_tier": "base"},
            risk_distance=9.0,
            risk_distance_pct=9.0,
            planned_notional=100.0,
            qty=1.0,
        ),
        free_balance=100.0,
        account_equity=100.0,
    )

    assert updated["small_account_aggressive_active"] is True
    assert updated["small_account_aggressive_blocked"] is False
    assert updated["leverage"] == 4
    assert updated["planned_margin"] < 61.75
    assert updated["qty"] > 0.0
    assert updated["small_account_aggressive_full_target_loss_percent"] <= 8.0 + 1e-9
    assert updated["small_account_stop_distance_risk_sizing_applied"] is True


def test_adaptive_trend_small_account_blocks_only_when_stop_breaks_liquidation_buffer():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=70.0,
            adaptive_trend_risk_tier="base",
            adaptive_breakout_trend_metrics={"risk_tier": "base"},
            risk_distance=20.0,
            risk_distance_pct=20.0,
            planned_notional=100.0,
            qty=1.0,
        ),
        free_balance=100.0,
        account_equity=100.0,
    )

    assert updated["small_account_aggressive_active"] is True
    assert updated["small_account_aggressive_blocked"] is True
    assert updated["small_account_aggressive_block_code"] == (
        "ENTRY_BLOCKED_SMALL_ACCOUNT_AGGRESSIVE_RISK"
    )
    assert updated["qty"] == pytest.approx(0.0)


def test_adaptive_trend_small_account_notional_is_inverse_to_stop_distance():
    def size_for_stop(stop_pct):
        return apply_dynamic_leverage_to_plan(
            _plan(
                strategy="adaptive_breakout_trend_v1",
                adaptive_breakout_trend_score=70.0,
                adaptive_trend_risk_tier="base",
                adaptive_breakout_trend_metrics={"risk_tier": "base"},
                risk_distance=stop_pct,
                risk_distance_pct=stop_pct,
                planned_notional=100.0,
                qty=1.0,
            ),
            free_balance=100.0,
            account_equity=100.0,
        )

    tight = size_for_stop(2.0)
    wide = size_for_stop(6.0)

    assert tight["small_account_stop_distance_risk_sizing_applied"] is True
    assert wide["small_account_stop_distance_risk_sizing_applied"] is True
    assert wide["adaptive_trend_target_notional"] < tight["adaptive_trend_target_notional"]
    assert tight["adaptive_trend_target_notional"] * 0.022 == pytest.approx(8.0)
    assert wide["adaptive_trend_target_notional"] * 0.062 == pytest.approx(8.0)


def test_adaptive_trend_small_account_ignores_daily_loss_for_new_risk():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=96.0,
            adaptive_trend_risk_tier="elite",
            adaptive_breakout_trend_metrics={"risk_tier": "elite"},
            risk_distance=1.0,
            risk_distance_pct=1.0,
            planned_notional=100.0,
            qty=1.0,
            small_account_aggressive_daily_pnl_usdt=-34.5,
        ),
        free_balance=100.0,
        account_equity=100.0,
    )

    assert updated["small_account_aggressive_blocked"] is False
    assert updated["small_account_aggressive_daily_loss_limit_usdt"] == pytest.approx(0.0)
    assert updated["qty"] > 0.0


def test_adaptive_trend_at_exact_threshold_keeps_normal_risk_sizing():
    updated = apply_dynamic_leverage_to_plan(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=70.0,
            planned_notional=300.0,
            qty=3.0,
        ),
        free_balance=1_000.0,
        account_equity=1_000.0,
    )

    assert updated["small_account_aggressive_active"] is False
    assert updated["planned_notional"] == pytest.approx(300.0)


def test_adaptive_trend_stop_buffer_caps_even_an_elite_override():
    decision = select_dynamic_leverage(
        _plan(
            strategy="adaptive_breakout_trend_v1",
            adaptive_breakout_trend_score=97.0,
            atr_pct=1.5,
            risk_distance_pct=5.0,
            market_quality_risk_multiplier=0.90,
            l2_risk_multiplier=0.90,
            l2_gate={
                "allowed": True,
                "state": "deep_balanced",
                "risk_multiplier": 0.90,
            },
        ),
        {
            "adaptive_trend_elite_stop_pct_max": 6.0,
            "adaptive_trend_stop_buffer_multiple": 2.5,
        },
    )

    assert decision.tier == "adaptive_trend_elite"
    assert decision.leverage == 8
    assert "stopBufferCap=8x" in decision.reason

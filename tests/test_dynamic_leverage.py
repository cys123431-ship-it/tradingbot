from utbreakout.dynamic_leverage import (
    apply_dynamic_leverage_to_plan,
    normalize_dynamic_leverage_config,
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


def test_disabled_mode_preserves_configured_plan_leverage_and_exit_profile():
    updated = apply_dynamic_leverage_to_plan(
        _plan(leverage=5, ev_time_stop_bars=96, atr_trailing_activation_r=1.6),
        {"enabled": False},
    )

    assert updated["leverage"] == 5
    assert updated["ev_time_stop_bars"] == 96
    assert updated["atr_trailing_activation_r"] == 1.6


def test_adaptive_trend_uses_ten_x_only_for_elite_tight_stop_setup():
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

    assert decision.leverage == 10
    assert decision.tier == "adaptive_trend_elite"
    assert decision.stop_distance_pct == 2.5


def test_adaptive_trend_uses_eight_x_for_strong_normal_volatility_setup():
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

    assert decision.leverage == 8
    assert decision.tier == "adaptive_trend_strong"


def test_adaptive_trend_uses_six_x_for_healthy_opportunity_setup():
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

    assert decision.leverage == 6
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


def test_adaptive_trend_high_leverage_applies_short_exit_profile():
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

    assert updated["leverage"] == 10
    assert updated["ev_time_stop_bars"] == 8
    assert updated["ev_time_stop_min_mfe_r"] == 0.70
    assert updated["atr_trailing_activation_r"] == 1.00
    assert updated["dynamic_leverage_monitor_timeframe"] == "15m"


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

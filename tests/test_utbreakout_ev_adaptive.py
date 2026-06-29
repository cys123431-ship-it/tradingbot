from utbreakout.ev_adaptive import (
    adapt_exit_for_quantity,
    evaluate_ev_time_stop,
    evaluate_ev_adaptive_entry,
    evaluate_mfe_profit_lock,
    evaluate_net_edge,
    rank_ev_candidates,
)
from emas import SignalEngine, apply_profit_opportunity_effective_overrides


def test_strong_trend_uses_convex_exit_without_increasing_initial_risk():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 100.0,
            "open": 99.4,
            "ema50": 98.5,
            "ema50_prev": 98.0,
            "htf_ready": True,
            "htf_close": 105.0,
            "htf_ema_fast": 102.0,
            "htf_ema_slow": 100.0,
            "htf_supertrend_direction": "long",
            "adx": 31.0,
            "plus_di": 30.0,
            "minus_di": 14.0,
            "chop": 39.0,
            "volume_ratio": 1.45,
            "directional_efficiency": 0.42,
            "momentum_12_pct": 2.4,
            "atr_pct": 0.8,
            "futures_spread_pct": 0.02,
            "taker_buy_sell_ratio": 1.08,
            "funding_rate": 0.0002,
        },
    )

    assert decision.allowed is True
    assert decision.mode == "STRONG_TREND"
    assert decision.risk_multiplier <= 1.0
    assert decision.exit_profile.tp1_r == 1.0
    assert decision.exit_profile.tp2_r == 3.5
    assert decision.exit_profile.runner_ratio == 0.45


def test_short_momentum_is_blocked_during_panic_rebound():
    decision = evaluate_ev_adaptive_entry(
        side="short",
        candidate_type="bias_continuation",
        values={
            "entry_price": 90.0,
            "ema50": 92.0,
            "ema50_prev": 93.0,
            "htf_ready": True,
            "htf_close": 88.0,
            "htf_ema_fast": 90.0,
            "htf_ema_slow": 94.0,
            "adx": 28.0,
            "plus_di": 12.0,
            "minus_di": 29.0,
            "chop": 42.0,
            "volume_ratio": 1.3,
            "directional_efficiency": 0.35,
            "momentum_12_pct": -3.0,
            "recent_rebound_pct": 7.0,
            "atr_pct": 1.7,
            "futures_spread_pct": 0.02,
        },
    )

    assert decision.allowed is False
    assert decision.mode == "NO_TRADE"
    assert any("panic rebound" in blocker for blocker in decision.blockers)


def test_short_relaxation_allows_small_aligned_downtrend_size():
    decision = evaluate_ev_adaptive_entry(
        side="short",
        candidate_type="fresh_signal",
        values={
            "entry_price": 100.0,
            "open": 100.6,
            "ema50": 101.0,
            "ema50_prev": 101.5,
            "htf_ready": True,
            "htf_close": 99.5,
            "htf_ema_fast": 100.0,
            "htf_ema_slow": 101.0,
            "htf_supertrend_direction": "short",
            "adx": 15.0,
            "plus_di": 14.0,
            "minus_di": 24.0,
            "chop": 50.0,
            "volume_ratio": 0.55,
            "directional_efficiency": 0.18,
            "momentum_6_pct": -0.4,
            "momentum_12_pct": -0.8,
            "momentum_24_pct": -1.3,
            "atr_pct": 0.9,
            "futures_spread_pct": 0.02,
            "recent_rebound_pct": 1.0,
            "mtf_metrics": {
                "15m": {"ema_bias": "short"},
                "30m": {"ema_bias": "short"},
                "1h": {"ema_bias": "short"},
            },
        },
    )

    assert decision.allowed is True
    assert decision.mode == "TREND"
    assert decision.risk_multiplier <= 0.45
    assert "short relaxation risk cap" in decision.reasons


def test_short_relaxation_does_not_override_rebound_protection():
    decision = evaluate_ev_adaptive_entry(
        side="short",
        candidate_type="fresh_signal",
        values={
            "entry_price": 100.0,
            "ema50": 101.0,
            "ema50_prev": 101.5,
            "htf_supertrend_direction": "short",
            "adx": 15.0,
            "plus_di": 14.0,
            "minus_di": 24.0,
            "chop": 50.0,
            "volume_ratio": 0.55,
            "directional_efficiency": 0.18,
            "momentum_6_pct": -0.4,
            "momentum_12_pct": -0.8,
            "momentum_24_pct": -1.3,
            "recent_rebound_pct": 7.0,
            "mtf_metrics": {
                "15m": {"ema_bias": "short"},
                "30m": {"ema_bias": "short"},
                "1h": {"ema_bias": "short"},
            },
        },
    )

    assert decision.allowed is False
    assert any("panic rebound" in blocker for blocker in decision.blockers)


def test_squeeze_release_gets_its_own_exit_profile():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 101.0,
            "ema50": 99.0,
            "ema50_prev": 98.8,
            "htf_ready": True,
            "htf_close": 103.0,
            "htf_ema_fast": 101.0,
            "htf_ema_slow": 99.0,
            "adx": 21.0,
            "plus_di": 25.0,
            "minus_di": 16.0,
            "chop": 47.0,
            "volume_ratio": 1.35,
            "directional_efficiency": 0.20,
            "momentum_12_pct": 1.2,
            "bb_width_percentile": 14.0,
            "range_expansion_ratio": 1.25,
            "donchian_high_prev": 100.0,
            "atr_pct": 0.7,
            "futures_spread_pct": 0.02,
        },
    )

    assert decision.allowed is True
    assert decision.mode == "SQUEEZE_BREAKOUT"
    assert decision.exit_profile.tp1_r == 1.2
    assert decision.exit_profile.tp2_r == 2.8


def test_cost_gate_rejects_positive_gross_edge_when_fees_consume_it():
    result = evaluate_net_edge(
        risk_usdt=0.10,
        planned_notional=100.0,
        win_probability=0.45,
        gross_win_r=1.8,
    )

    assert result.allowed is False
    assert result.cost_r > 1.0
    assert result.expected_net_r < 0.0


def test_small_position_uses_single_target_instead_of_collapsing_to_tp1():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 100.0,
            "ema50": 99.0,
            "ema50_prev": 98.0,
            "htf_supertrend_direction": "long",
            "adx": 22.0,
            "plus_di": 25.0,
            "minus_di": 15.0,
            "chop": 45.0,
            "volume_ratio": 1.0,
            "directional_efficiency": 0.20,
            "momentum_12_pct": 1.0,
        },
    )

    adapted = adapt_exit_for_quantity(
        decision.exit_profile,
        total_qty=0.01,
        min_amount=0.01,
    )

    assert adapted.mode == "SINGLE_TARGET"
    assert adapted.profile.tp1_ratio == 1.0
    assert adapted.profile.tp1_r == decision.exit_profile.single_target_r
    assert adapted.profile.tp2_ratio == 0.0
    assert adapted.profile.runner_ratio == 0.0


def test_time_stop_only_exits_when_trade_has_no_follow_through():
    weak = evaluate_ev_time_stop(
        bars_held=8,
        mfe_r=0.30,
        tp1_filled=False,
        max_bars=8,
        min_mfe_r=0.45,
    )
    progressing = evaluate_ev_time_stop(
        bars_held=8,
        mfe_r=0.80,
        tp1_filled=False,
        max_bars=8,
        min_mfe_r=0.45,
    )

    assert weak.should_exit is True
    assert progressing.should_exit is False


def test_candidate_ranking_prefers_net_edge_over_legacy_indicator_score():
    ranked = rank_ev_candidates(
        [
            {"symbol": "OLD/USDT", "score": 90.0, "ev_net_edge_r": 0.05, "ev_allowed": True},
            {"symbol": "EDGE/USDT", "score": 70.0, "ev_net_edge_r": 0.28, "ev_allowed": True},
            {"symbol": "BLOCKED/USDT", "score": 99.0, "ev_net_edge_r": 0.50, "ev_allowed": False},
        ]
    )

    assert [item["symbol"] for item in ranked] == ["EDGE/USDT", "OLD/USDT"]


def test_live_effective_profile_retires_legacy_set_selection_and_risk_floor():
    cfg = apply_profit_opportunity_effective_overrides({})

    assert cfg["effective_profile_version"] == "ev_adaptive_v2"
    assert cfg["live_auto_set_whitelist"] == [64]
    assert cfg["active_set_id"] == 64
    assert cfg["final_risk_multiplier_floor"] == 0.0
    assert cfg["partial_take_profit_ratio"] == 0.30
    assert cfg["second_take_profit_r_multiple"] == 2.0


def test_live_auto_selection_uses_only_ev_adaptive_set():
    engine = object.__new__(SignalEngine)
    cfg = apply_profit_opportunity_effective_overrides(
        {"live_trading": True, "mode": "live"}
    )
    analysis = {
        "scores": {
            "ready_timeframes": 3,
            "trend_score": 80.0,
            "breakout_score": 70.0,
            "momentum_score": 75.0,
        }
    }

    selected, reason = engine._select_utbreakout_auto_set(analysis, cfg)

    assert selected == 64
    assert "EV Adaptive" in reason
    assert analysis["scores"]["auto_final_set_id"] == 64


def test_live_auto_selection_reason_says_ev_adaptive_v2():
    engine = object.__new__(SignalEngine)
    cfg = apply_profit_opportunity_effective_overrides(
        {"live_trading": True, "mode": "live"}
    )
    selected, reason = engine._select_utbreakout_auto_set({"scores": {}}, cfg)

    assert selected == 64
    assert "EV Adaptive V2" in reason
    assert "EV Adaptive V1" not in reason


def test_live_trailing_state_preserves_normal_ev_runner_without_growth_overlay():
    class Exchange:
        @staticmethod
        def market(_symbol):
            return {"limits": {"amount": {"min": 0.01}}}

    engine = object.__new__(SignalEngine)
    engine.exchange = Exchange()
    engine.safe_amount = lambda _symbol, qty: qty
    engine.utbreakout_trailing_states = {}
    plan = {
        "risk_distance": 10.0,
        "stop_loss": 90.0,
        "partial_take_profit_ratio": 0.20,
        "partial_take_profit_r_multiple": 1.0,
        "second_take_profit_ratio": 0.35,
        "second_take_profit_r_multiple": 3.5,
        "runner_pct": 0.45,
        "atr_trailing_enabled": True,
        "atr_trailing_activation_r": 1.3,
        "atr_trailing_multiplier": 3.2,
        "runner_exit_enabled": True,
        "runner_chandelier_enabled": True,
        "ev_adaptive_enabled": True,
        "ev_adaptive_mode": "STRONG_TREND",
        "ev_exit_profile_name": "STRONG_TREND",
        "ev_time_stop_enabled": True,
        "ev_time_stop_bars": 10,
        "ev_time_stop_min_mfe_r": 0.45,
        "aggressive_growth_overlay": False,
    }

    state = engine._register_utbreakout_trailing_state(
        "BTC/USDT",
        "long",
        100.0,
        1.0,
        plan,
        {},
    )

    assert state["preserve_runner_qty"] is True
    assert state["remaining_ratio"] == 0.45
    assert state["tp1_expected_remaining_ratio"] == 0.80
    assert [round(item["qty"], 2) for item in state["planned_tp_orders"]] == [0.20, 0.35]
    assert state["ev_time_stop_bars"] == 10
    assert state["ev_mfe_profit_lock_enabled"] is True


def test_stale_continuation_requires_real_reacceleration():
    values = {
        "entry_price": 100.0,
        "open": 99.8,
        "ema50": 99.0,
        "ema50_prev": 98.8,
        "htf_supertrend_direction": "long",
        "adx": 24.0,
        "plus_di": 27.0,
        "minus_di": 15.0,
        "chop": 44.0,
        "volume_ratio": 0.75,
        "directional_efficiency": 0.30,
        "momentum_6_pct": 0.5,
        "momentum_12_pct": 1.0,
        "momentum_24_pct": 1.8,
        "signal_age_candles": 18.0,
        "range_expansion_ratio": 0.95,
        "donchian_high_prev": 101.0,
    }

    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="bias_continuation",
        values=values,
    )

    assert decision.allowed is False
    assert any("stale continuation" in blocker for blocker in decision.blockers)


def test_stale_continuation_can_be_renewed_by_breakout_reacceleration():
    values = {
        "entry_price": 102.0,
        "open": 100.5,
        "ema50": 99.0,
        "ema50_prev": 98.8,
        "htf_supertrend_direction": "long",
        "adx": 27.0,
        "plus_di": 30.0,
        "minus_di": 14.0,
        "chop": 42.0,
        "volume_ratio": 1.25,
        "directional_efficiency": 0.38,
        "momentum_6_pct": 0.8,
        "momentum_12_pct": 1.5,
        "momentum_24_pct": 2.4,
        "signal_age_candles": 18.0,
        "range_expansion_ratio": 1.25,
        "donchian_high_prev": 101.0,
        "mtf_metrics": {
            "15m": {"ema_bias": "long"},
            "30m": {"ema_bias": "long"},
            "1h": {"ema_bias": "long"},
        },
    }

    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="bias_continuation",
        values=values,
    )

    assert decision.allowed is True
    assert decision.reacceleration is True
    assert decision.risk_multiplier < 1.0


def test_mtf_conflict_blocks_otherwise_valid_entry():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 101.0,
            "ema50": 99.0,
            "ema50_prev": 98.5,
            "adx": 28.0,
            "plus_di": 30.0,
            "minus_di": 12.0,
            "chop": 42.0,
            "volume_ratio": 1.25,
            "directional_efficiency": 0.35,
            "momentum_6_pct": 0.7,
            "momentum_12_pct": 1.2,
            "momentum_24_pct": 2.0,
            "mtf_metrics": {
                "15m": {"ema_bias": "long"},
                "30m": {"ema_bias": "short"},
                "1h": {"ema_bias": "short"},
            },
        },
    )

    assert decision.allowed is False
    assert any("MTF alignment" in blocker for blocker in decision.blockers)


def test_mtf_zero_of_three_is_not_relieved():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 105.0,
            "ema50": 103.0,
            "ema50_prev": 102.5,
            "adx": 32.0,
            "plus_di": 31.0,
            "minus_di": 12.0,
            "chop": 38.0,
            "volume_ratio": 1.45,
            "directional_efficiency": 0.42,
            "momentum_6_pct": 0.8,
            "momentum_12_pct": 1.5,
            "momentum_24_pct": 2.4,
            "donchian_high_prev": 104.0,
            "range_expansion_ratio": 1.30,
            "mtf_metrics": {
                "15m": {"ema_bias": "short"},
                "30m": {"ema_bias": "short"},
                "1h": {"ema_bias": "short"},
            },
        },
    )
    assert decision.allowed is False
    assert any("MTF alignment" in blocker for blocker in decision.blockers)


def test_mtf_one_of_three_can_be_relieved_by_strong_breakout_substitute():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 105.0,
            "ema50": 103.0,
            "ema50_prev": 102.5,
            "adx": 32.0,
            "plus_di": 31.0,
            "minus_di": 12.0,
            "chop": 38.0,
            "volume_ratio": 1.45,
            "directional_efficiency": 0.42,
            "momentum_6_pct": 0.8,
            "momentum_12_pct": 1.5,
            "momentum_24_pct": 2.4,
            "donchian_high_prev": 104.0,
            "range_expansion_ratio": 1.30,
            "mtf_metrics": {
                "15m": {"ema_bias": "long"},
                "30m": {"ema_bias": "short"},
                "1h": {"ema_bias": "short"},
            },
        },
    )
    assert decision.allowed is True
    assert not any("MTF alignment" in blocker for blocker in decision.blockers)
    assert any("OR relief: MTF" in reason for reason in decision.reasons)
    assert 0 < decision.risk_multiplier <= 0.55


def test_stale_continuation_can_be_conditionally_relieved_with_size_cap():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="bias_continuation",
        values={
            "entry_price": 105.0,
            "ema50": 103.0,
            "ema50_prev": 102.5,
            "adx": 30.0,
            "plus_di": 31.0,
            "minus_di": 12.0,
            "chop": 40.0,
            "volume_ratio": 1.35,
            "directional_efficiency": 0.36,
            "momentum_6_pct": 0.8,
            "momentum_12_pct": 1.5,
            "momentum_24_pct": 2.4,
            "signal_age_candles": 18.0,
            "donchian_high_prev": 106.0,
            "range_expansion_ratio": 1.20,
            "mtf_metrics": {
                "15m": {"ema_bias": "long"},
                "30m": {"ema_bias": "long"},
                "1h": {"ema_bias": "short"},
            },
        },
    )
    assert decision.allowed is True
    assert not any("stale continuation" in blocker for blocker in decision.blockers)
    assert any("OR relief: stale continuation" in reason for reason in decision.reasons)
    assert 0 < decision.risk_multiplier <= 0.55


def test_no_edge_can_be_relieved_only_by_strict_breakout_substitute():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 105.0,
            "ema50": 103.0,
            "ema50_prev": 102.5,
            "adx": 28.0,
            "plus_di": 31.0,
            "minus_di": 12.0,
            "chop": 64.0,  # prevents normal TREND because trend_max_chop is 60
            "volume_ratio": 1.45,
            "directional_efficiency": 0.36,
            "momentum_6_pct": 0.8,
            "momentum_12_pct": 1.5,
            "momentum_24_pct": 2.4,
            "donchian_high_prev": 104.0,
            "range_expansion_ratio": 1.25,
            "mtf_metrics": {
                "15m": {"ema_bias": "long"},
                "30m": {"ema_bias": "short"},
                "1h": {"ema_bias": "short"},
            },
        },
    )
    assert decision.allowed is True
    assert decision.mode == "TREND"
    assert not any("no trend or squeeze edge" in blocker for blocker in decision.blockers)
    assert any("OR relief: no trend/squeeze edge" in reason for reason in decision.reasons)
    assert 0 < decision.risk_multiplier <= 0.55


def test_conditional_relief_does_not_bypass_extreme_volatility_or_spread():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 105.0,
            "ema50": 103.0,
            "ema50_prev": 102.5,
            "adx": 35.0,
            "plus_di": 35.0,
            "minus_di": 10.0,
            "chop": 35.0,
            "volume_ratio": 1.60,
            "directional_efficiency": 0.45,
            "momentum_6_pct": 1.0,
            "momentum_12_pct": 2.0,
            "momentum_24_pct": 3.0,
            "donchian_high_prev": 104.0,
            "range_expansion_ratio": 1.40,
            "futures_spread_pct": 0.20,
            "atr_pct": 3.0,
            "mtf_metrics": {
                "15m": {"ema_bias": "long"},
                "30m": {"ema_bias": "long"},
                "1h": {"ema_bias": "long"},
            },
        },
    )
    assert decision.allowed is False
    assert any("spread" in blocker or "extreme volatility" in blocker for blocker in decision.blockers)


def test_cross_sectional_leader_receives_more_size_without_exceeding_base_risk():
    base = {
        "entry_price": 101.0,
        "ema50": 99.0,
        "ema50_prev": 98.5,
        "htf_supertrend_direction": "long",
        "adx": 25.0,
        "plus_di": 28.0,
        "minus_di": 15.0,
        "chop": 45.0,
        "volume_ratio": 1.10,
        "directional_efficiency": 0.30,
        "momentum_6_pct": 0.6,
        "momentum_12_pct": 1.1,
        "momentum_24_pct": 1.8,
    }
    leader = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            **base,
            "cross_sectional_rank_pct": 100.0,
            "selector_return_lookback_pct": 6.0,
            "selector_rolling_sharpe": 1.2,
            "selector_momentum_consistency": 0.68,
            "selector_directional_efficiency": 0.48,
        },
    )
    middle = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            **base,
            "cross_sectional_rank_pct": 50.0,
            "selector_return_lookback_pct": 0.5,
            "selector_rolling_sharpe": 0.1,
            "selector_momentum_consistency": 0.52,
            "selector_directional_efficiency": 0.18,
        },
    )

    assert leader.allowed is True
    assert leader.leadership_score > middle.leadership_score
    assert leader.risk_multiplier >= middle.risk_multiplier
    assert leader.risk_multiplier <= 1.0


def test_mfe_profit_lock_raises_locked_profit_in_stages():
    inactive = evaluate_mfe_profit_lock(mfe_r=1.40, mode="TREND")
    first = evaluate_mfe_profit_lock(mfe_r=1.60, mode="TREND")
    second = evaluate_mfe_profit_lock(mfe_r=2.30, mode="TREND")
    third = evaluate_mfe_profit_lock(mfe_r=3.40, mode="TREND")

    assert inactive.active is False
    assert (first.stage, first.lock_r) == (1, 0.40)
    assert (second.stage, second.lock_r) == (2, 1.00)
    assert (third.stage, third.lock_r) == (3, 1.80)


def test_derivatives_crowding_blocks_late_long_chase():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 105.0,
            "ema50": 101.0,
            "ema50_prev": 100.4,
            "htf_supertrend_direction": "long",
            "adx": 32.0,
            "plus_di": 35.0,
            "minus_di": 12.0,
            "chop": 38.0,
            "volume_ratio": 1.55,
            "directional_efficiency": 0.46,
            "momentum_6_pct": 1.3,
            "momentum_12_pct": 2.2,
            "momentum_24_pct": 3.8,
            "funding_rate": 0.0020,
            "funding_percentile_7d": 96.0,
            "long_short_ratio": 2.65,
            "open_interest_delta_pct": 1.20,
            "price_change_1h": 0.04,
            "rolling_ofi_score": 4.0,
            "rolling_ofi_samples": 5,
            "taker_buy_sell_ratio": 1.06,
        },
    )

    assert decision.allowed is False
    assert any("derivatives crowding" in blocker or "crowded OI stall" in blocker for blocker in decision.blockers)


def test_derivatives_confirmation_improves_score_and_size():
    base_values = {
        "entry_price": 101.0,
        "ema50": 99.0,
        "ema50_prev": 98.7,
        "htf_supertrend_direction": "long",
        "adx": 27.0,
        "plus_di": 29.0,
        "minus_di": 14.0,
        "chop": 43.0,
        "volume_ratio": 1.18,
        "directional_efficiency": 0.34,
        "momentum_6_pct": 0.7,
        "momentum_12_pct": 1.4,
        "momentum_24_pct": 2.1,
    }
    base = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values=base_values,
    )
    confirmed = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            **base_values,
            "funding_rate": -0.0002,
            "long_short_ratio": 1.15,
            "open_interest_delta_pct": 0.90,
            "open_interest_change_4h": 1.80,
            "price_change_1h": 0.55,
            "price_change_4h": 1.10,
            "rolling_ofi_score": 5.5,
            "rolling_ofi_samples": 5,
            "taker_buy_sell_ratio": 1.08,
            "liquidation_imbalance": 0.35,
        },
    )

    assert confirmed.allowed is True
    assert confirmed.score > base.score
    assert confirmed.risk_multiplier >= base.risk_multiplier
    assert any("OI confirms" in reason for reason in confirmed.reasons)
    assert any("orderflow confirms" in reason for reason in confirmed.reasons)


def test_opposite_orderflow_reduces_otherwise_valid_entry():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="fresh_signal",
        values={
            "entry_price": 102.0,
            "ema50": 99.5,
            "ema50_prev": 99.0,
            "htf_supertrend_direction": "long",
            "adx": 29.0,
            "plus_di": 32.0,
            "minus_di": 13.0,
            "chop": 41.0,
            "volume_ratio": 1.25,
            "directional_efficiency": 0.39,
            "momentum_6_pct": 0.8,
            "momentum_12_pct": 1.6,
            "momentum_24_pct": 2.5,
            "rolling_ofi_score": -7.2,
            "rolling_ofi_samples": 5,
            "taker_buy_sell_ratio": 0.93,
        },
    )

    assert decision.allowed is True
    assert decision.risk_multiplier < 0.60
    assert not any("opposite orderflow" in blocker for blocker in decision.blockers)
    assert any("opposite orderflow" in reason for reason in decision.reasons)


def test_ev_adaptive_continuation_requires_signal_age():
    decision = evaluate_ev_adaptive_entry(
        side="short",
        candidate_type="bias_state",
        values={
            "entry_price": 100.0,
            "ema50": 101.0,
            "ema50_prev": 101.5,
            "htf_close": 98.0,
            "htf_ema_fast": 99.0,
            "htf_ema_slow": 101.0,
            "adx": 32.0,
            "plus_di": 12.0,
            "minus_di": 31.0,
            "chop": 38.0,
            "volume_ratio": 1.45,
            "directional_efficiency": 0.42,
            "momentum_6_pct": -0.8,
            "momentum_12_pct": -1.5,
            "momentum_24_pct": -2.4,
            "donchian_low_prev": 101.0,
            "range_expansion_ratio": 1.30,
            "mtf_metrics": {
                "15m": {"ema_bias": "short"},
                "30m": {"ema_bias": "short"},
                "1h": {"ema_bias": "short"},
            },
        },
    )
    assert decision.allowed is False
    assert any("signal age missing" in blocker for blocker in decision.blockers)
    assert decision.risk_multiplier == 0


def test_ev_adaptive_continuation_with_signal_age_can_size_short():
    decision = evaluate_ev_adaptive_entry(
        side="short",
        candidate_type="bias_state",
        values={
            "entry_price": 100.0,
            "ema50": 101.0,
            "ema50_prev": 101.5,
            "htf_close": 98.0,
            "htf_ema_fast": 99.0,
            "htf_ema_slow": 101.0,
            "adx": 32.0,
            "plus_di": 12.0,
            "minus_di": 31.0,
            "chop": 38.0,
            "volume_ratio": 1.45,
            "directional_efficiency": 0.42,
            "momentum_6_pct": -0.8,
            "momentum_12_pct": -1.5,
            "momentum_24_pct": -2.4,
            "signal_age_candles": 2.0,
            "donchian_low_prev": 101.0,
            "range_expansion_ratio": 1.30,
            "mtf_metrics": {
                "15m": {"ema_bias": "short"},
                "30m": {"ema_bias": "short"},
                "1h": {"ema_bias": "short"},
            },
        },
    )
    assert decision.allowed is True
    assert decision.risk_multiplier > 0


def test_ev_adaptive_continuation_with_signal_age_can_size_long():
    decision = evaluate_ev_adaptive_entry(
        side="long",
        candidate_type="bias_state",
        values={
            "entry_price": 100.0,
            "ema50": 99.0,
            "ema50_prev": 98.5,
            "htf_close": 102.0,
            "htf_ema_fast": 101.0,
            "htf_ema_slow": 99.0,
            "adx": 32.0,
            "plus_di": 31.0,
            "minus_di": 12.0,
            "chop": 38.0,
            "volume_ratio": 1.45,
            "directional_efficiency": 0.42,
            "momentum_6_pct": 0.8,
            "momentum_12_pct": 1.5,
            "momentum_24_pct": 2.4,
            "signal_age_candles": 2.0,
            "donchian_high_prev": 99.0,
            "range_expansion_ratio": 1.30,
            "mtf_metrics": {
                "15m": {"ema_bias": "long"},
                "30m": {"ema_bias": "long"},
                "1h": {"ema_bias": "long"},
            },
        },
    )
    assert decision.allowed is True
    assert decision.risk_multiplier > 0

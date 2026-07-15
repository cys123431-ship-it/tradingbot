from dataclasses import replace
from types import SimpleNamespace

from utbreakout.alpha_engine import (
    apply_profit_alpha_exit_overrides,
    build_entry_edge_decision,
    evaluate_alpha_follow_through_exit,
    evaluate_profit_alpha,
)


def _base_values(side="long"):
    return {
        "adx": 32.0,
        "chop": 35.0,
        "plus_di": 28.0 if side == "long" else 12.0,
        "minus_di": 12.0 if side == "long" else 28.0,
        "close": 110.0 if side == "long" else 90.0,
        "ema50": 100.0,
        "ema200": 95.0 if side == "long" else 105.0,
        "htf_close": 110.0 if side == "long" else 90.0,
        "htf_ema_slow": 100.0,
        "volume_ratio": 1.55,
        "range_expansion": 1.25,
        "coin_selector_score": 82.0,
        "trend_health_score": 84.0,
        "strategy_quality_score": 78.0,
        "quality_score_v2_score": 81.0,
        "taker_buy_sell_ratio": 1.08 if side == "long" else 0.92,
        "rolling_ofi": 18.0 if side == "long" else -18.0,
        "orderbook_imbalance_pct": 14.0 if side == "long" else -14.0,
        "open_interest_change_1h": 0.48,
        "open_interest_acceleration": 0.05 if side == "long" else -0.05,
        "funding_rate": -0.0001 if side == "long" else 0.0001,
        "basis_pct": 0.02,
        "long_short_ratio": 1.10,
        "bb_width_percentile": 28.0,
        "keltner_squeeze_state": "OFF",
        "squeeze_state": "inactive",
        "market_regime_context": {
            "items": {
                "BTC/USDT": {
                    "direction": side,
                    "return_lookback_pct": 0.9 if side == "long" else -0.9,
                },
                "ETH/USDT": {
                    "direction": side,
                    "return_lookback_pct": 0.7 if side == "long" else -0.7,
                },
            }
        },
    }


def _ev(mode="STRONG_TREND"):
    return SimpleNamespace(
        allowed=True,
        mode=mode,
        score=78.0,
        win_probability=0.58,
        risk_multiplier=0.82,
        mtf_alignment="3/3",
        leadership_score=78.0,
        signal_age_candles=3.0,
        reacceleration=True,
        reasons=("EV trend edge",),
        blockers=(),
    )


def test_profit_alpha_allows_strong_long_trend():
    decision = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=_ev(),
    )

    assert decision.allowed is True
    assert decision.engine in {"STRONG_UPTREND_LONG", "TREND_CONTINUATION"}
    assert decision.score >= 68.0
    assert decision.probability >= 0.555
    assert decision.direction_score >= 62.0
    assert decision.entry_type in {"BREAKOUT", "TREND_CONTINUATION", "PULLBACK_RETEST"}
    assert decision.exit_policy


def test_profit_alpha_allows_strong_short_downtrend():
    decision = evaluate_profit_alpha(
        side="short",
        values=_base_values("short"),
        ev_decision=_ev(),
    )

    assert decision.allowed is True
    assert decision.engine in {"STRONG_DOWNTREND_SHORT", "TREND_CONTINUATION"}
    assert decision.score >= 68.0
    assert decision.direction_score >= 62.0


def test_profit_alpha_blocks_opposite_market_regime_when_edge_is_not_strong():
    values = _base_values("long")
    values.update({
        "adx": 22.0,
        "volume_ratio": 1.05,
        "range_expansion": 1.02,
        "coin_selector_score": 58.0,
        "trend_health_score": 62.0,
        "strategy_quality_score": 60.0,
        "quality_score_v2_score": 61.0,
        "market_regime_context": {
            "items": {
                "BTC/USDT": {"direction": "short", "return_lookback_pct": -1.9},
                "ETH/USDT": {"direction": "short", "return_lookback_pct": -1.4},
            }
        },
    })

    decision = evaluate_profit_alpha(
        side="long",
        values=values,
        ev_decision=_ev("TREND"),
        config={"utbreak_entry_relaxation_mode": "strict"},
    )

    assert decision.allowed is False
    assert any("top market regime opposite" in item for item in decision.blockers)


def test_profit_alpha_balanced_softens_weak_opposite_regime_with_size_reduction():
    values = _base_values("long")
    values["market_regime_context"] = {
        "items": {
            "BTC/USDT": {"direction": "short", "return_lookback_pct": -0.8},
            "ETH/USDT": {"direction": "short", "return_lookback_pct": -0.6},
        }
    }

    decision = evaluate_profit_alpha(
        side="long",
        values=values,
        ev_decision=_ev("TREND"),
        config={"utbreak_entry_relaxation_mode": "balanced"},
    )

    assert decision.allowed is True
    assert decision.risk_multiplier <= 0.65
    assert "regime_action=size_reduce" in decision.components["entry_relaxation_actions"]


def test_profit_alpha_strict_blocks_borderline_stale_signal():
    ev = _ev("TREND")
    ev.signal_age_candles = 10.0
    ev.reacceleration = False

    decision = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=ev,
        config={"utbreak_entry_relaxation_mode": "strict"},
    )

    assert decision.allowed is False
    assert any("stale signal" in item for item in decision.blockers)


def test_profit_alpha_balanced_softens_borderline_stale_signal():
    ev = _ev("TREND")
    ev.signal_age_candles = 10.0
    ev.reacceleration = False

    decision = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=ev,
        config={"utbreak_entry_relaxation_mode": "balanced"},
    )

    assert decision.allowed is True
    assert decision.risk_multiplier <= 0.65
    assert any("stale_signal" in item for item in decision.reasons)


def test_profit_alpha_active_allows_wider_stale_window_than_balanced():
    ev = _ev("TREND")
    ev.signal_age_candles = 14.0
    ev.reacceleration = False

    balanced = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=ev,
        config={"utbreak_entry_relaxation_mode": "balanced"},
    )
    active = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=ev,
        config={"utbreak_entry_relaxation_mode": "active"},
    )

    assert balanced.allowed is False
    assert active.allowed is True
    assert active.risk_multiplier <= 0.55


def test_profit_alpha_balanced_reduces_size_for_weak_adx_but_blocks_very_low_adx():
    weak = _base_values("long")
    weak["adx"] = 17.0
    very_low = _base_values("long")
    very_low["adx"] = 14.0

    weak_decision = evaluate_profit_alpha(
        side="long",
        values=weak,
        ev_decision=_ev("TREND"),
        config={
            "utbreak_entry_relaxation_mode": "balanced",
            "direction_engine_min_score": 0.0,
        },
    )
    low_decision = evaluate_profit_alpha(
        side="long",
        values=very_low,
        ev_decision=_ev("TREND"),
        config={
            "utbreak_entry_relaxation_mode": "balanced",
            "direction_engine_min_score": 0.0,
        },
    )

    assert weak_decision.allowed is True
    assert weak_decision.risk_multiplier <= 0.65
    assert any("adx_weak_size_reduced" in item for item in weak_decision.reasons)
    assert low_decision.allowed is False
    assert any("adx_too_low" in item for item in low_decision.blockers)


def test_profit_alpha_balanced_penalizes_missing_derivatives_without_hard_block():
    values = _base_values("short")
    for key in (
        "taker_buy_sell_ratio",
        "rolling_ofi",
        "orderbook_imbalance_pct",
        "open_interest_change_1h",
        "open_interest_acceleration",
        "funding_rate",
        "basis_pct",
        "long_short_ratio",
    ):
        values.pop(key, None)

    decision = evaluate_profit_alpha(
        side="short",
        values=values,
        ev_decision=_ev("TREND"),
        config={"utbreak_entry_relaxation_mode": "balanced"},
    )

    assert decision.allowed is True
    assert decision.risk_multiplier <= 0.80
    assert any("derivatives_data_missing" in item for item in decision.reasons)


def test_profit_alpha_blocks_derivatives_adverse_stack():
    values = _base_values("long")
    values.update({
        "taker_buy_sell_ratio": 0.88,
        "rolling_ofi": -24.0,
        "orderbook_imbalance_pct": -35.0,
        "open_interest_change_1h": 0.01,
        "funding_rate": 0.0012,
        "basis_pct": 0.35,
    })

    decision = evaluate_profit_alpha(
        side="long",
        values=values,
        ev_decision=_ev(),
    )

    assert decision.allowed is False
    assert any("derivatives adverse stack" in item for item in decision.blockers)


def test_profit_alpha_meta_performance_only_reduces_position_size():
    decision = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=_ev(),
        meta_stats={
            "long:STRONG_UPTREND_LONG": {
                "sample_count": 12,
                "expectancy_r": -0.20,
            }
        },
    )

    assert decision.allowed is True
    assert decision.risk_multiplier <= 0.70
    assert any("meta performance advisory" in item for item in decision.reasons)


def test_profit_alpha_exit_overrides_expand_strong_trend_runner():
    decision = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=_ev(),
    )

    cfg = apply_profit_alpha_exit_overrides({}, decision)

    assert cfg["runner_pct"] >= 0.40
    assert cfg["take_profit_r_multiple"] >= 2.8
    assert cfg["profit_alpha_follow_through_enabled"] is True
    assert cfg["soft_stop_enabled"] is True
    assert cfg["soft_stop_confirm_bars"] == 2
    assert cfg["structure_stop_buffer_atr"] >= 0.34
    assert cfg["near_miss_tp_enabled"] is True
    assert cfg["near_miss_tp_arm_ratio"] >= 0.94


def test_profit_alpha_splits_pullback_entry_type_and_exit_policy():
    values = _base_values("long")
    values.update({
        "extension_atr": 1.55,
        "range_expansion": 1.02,
    })

    decision = evaluate_profit_alpha(
        side="long",
        values=values,
        ev_decision=_ev(),
    )

    assert decision.allowed is True
    assert decision.entry_type == "PULLBACK_RETEST"
    assert decision.exit_policy == "PULLBACK_BALANCED_LADDER"
    cfg = apply_profit_alpha_exit_overrides({}, decision)
    assert cfg["second_take_profit_r_multiple"] == 2.45
    assert cfg["structure_stop_buffer_atr"] >= 0.32


def test_profit_alpha_sweep_exit_profile_requires_close_tp_approach():
    values = _base_values("long")
    values.update({
        "open": 108.0,
        "high": 111.0,
        "low": 100.0,
        "close": 110.0,
        "range_expansion": 1.10,
    })

    decision = evaluate_profit_alpha(
        side="long",
        values=values,
        ev_decision=_ev("TREND"),
    )

    cfg = apply_profit_alpha_exit_overrides({}, decision)
    assert decision.entry_type == "LIQUIDITY_SWEEP_REVERSAL"
    assert cfg["take_profit_front_run_pct"] >= 0.08
    assert cfg["near_miss_tp_arm_ratio"] >= 0.94
    assert cfg["near_miss_tp_lock_r"] >= 0.36
    assert cfg["soft_stop_confirm_bars"] == 1


def test_profit_alpha_exit_meta_only_reduces_position_size():
    values = _base_values("short")

    decision = evaluate_profit_alpha(
        side="short",
        values=values,
        ev_decision=_ev(),
        meta_stats={
            "short:STRONG_DOWNTREND_SHORT:TREND_RUNNER": {
                "sample_count": 10,
                "expectancy_r": -0.22,
            }
        },
    )

    assert decision.allowed is True
    assert decision.risk_multiplier <= 0.78
    assert any("exit meta advisory" in item for item in decision.reasons)


def test_entry_edge_combines_ev_and_profit_alpha_into_single_decision():
    alpha = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=_ev(),
    )

    decision = build_entry_edge_decision(
        side="long",
        ev_decision=_ev(),
        alpha_decision=alpha,
        ev_net=SimpleNamespace(allowed=True, expected_net_r=0.32, reason="ok"),
        ev_exit=SimpleNamespace(executable=True, reason="ok"),
    )

    assert decision.allowed is True
    assert decision.engine == alpha.engine
    assert decision.score >= 68.0
    assert decision.probability >= 0.555
    assert decision.net_expectancy_r == 0.32
    assert decision.risk_multiplier <= 0.82
    assert decision.entry_type == alpha.entry_type
    assert decision.exit_policy == alpha.exit_policy
    assert decision.direction_score == alpha.direction_score


def test_entry_edge_balanced_softens_borderline_thresholds_with_size_reduction():
    alpha = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=_ev(),
    )
    alpha = replace(alpha, allowed=True, score=68.0, probability=0.558, risk_multiplier=1.0, blockers=())
    ev = _ev()
    ev.score = 68.0
    ev.win_probability = 0.558
    ev.risk_multiplier = 1.0

    decision = build_entry_edge_decision(
        side="long",
        ev_decision=ev,
        alpha_decision=alpha,
        config={"utbreak_entry_relaxation_mode": "balanced"},
    )

    assert decision.allowed is True
    assert decision.risk_multiplier <= 0.60
    assert any("Entry Edge score borderline" in item for item in decision.reasons)
    assert any("Entry Edge p borderline" in item for item in decision.reasons)


def test_entry_edge_strict_blocks_same_borderline_thresholds():
    alpha = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=_ev(),
    )
    alpha = replace(alpha, allowed=True, score=68.0, probability=0.558, risk_multiplier=1.0, blockers=())
    ev = _ev()
    ev.score = 68.0
    ev.win_probability = 0.558
    ev.risk_multiplier = 1.0

    decision = build_entry_edge_decision(
        side="long",
        ev_decision=ev,
        alpha_decision=alpha,
        config={"utbreak_entry_relaxation_mode": "strict"},
    )

    assert decision.allowed is False
    assert any("Entry Edge score" in item for item in decision.blockers)
    assert any("Entry Edge p" in item for item in decision.blockers)


def test_entry_edge_blocks_when_either_source_is_not_allowed():
    alpha = evaluate_profit_alpha(
        side="long",
        values=_base_values("long"),
        ev_decision=_ev(),
    )
    weak_ev = _ev()
    weak_ev.allowed = False
    weak_ev.blockers = ("MTF alignment 0/3",)

    decision = build_entry_edge_decision(
        side="long",
        ev_decision=weak_ev,
        alpha_decision=alpha,
    )

    assert decision.allowed is False
    assert any(item.startswith("EV:") for item in decision.blockers)


def test_entry_edge_includes_net_edge_blocker():
    alpha = evaluate_profit_alpha(
        side="short",
        values=_base_values("short"),
        ev_decision=_ev(),
    )

    decision = build_entry_edge_decision(
        side="short",
        ev_decision=_ev(),
        alpha_decision=alpha,
        ev_net=SimpleNamespace(
            allowed=False,
            expected_net_r=0.05,
            reason="expected net too low",
        ),
    )

    assert decision.allowed is False
    assert any("Net edge" in item for item in decision.blockers)
    assert any("Entry Edge net" in item for item in decision.blockers)


def test_entry_edge_disabled_is_explicit_pass_through():
    decision = build_entry_edge_decision(
        side="long",
        ev_decision=None,
        alpha_decision=None,
        config={"entry_edge_enabled": False},
    )

    assert decision.allowed is True
    assert decision.engine == "DISABLED"
    assert decision.risk_multiplier == 1.0


def test_alpha_follow_through_exits_when_no_mfe_after_required_bars():
    result = evaluate_alpha_follow_through_exit(
        enabled=True,
        bars_held=3,
        mfe_r=0.12,
        mae_r=0.25,
        tp1_filled=False,
        follow_through_bars=3,
        follow_through_min_mfe_r=0.35,
        early_exit_max_mae_r=0.75,
    )

    assert result.should_exit is True
    assert "no follow-through" in result.reason


def test_alpha_follow_through_does_not_exit_after_tp1_fill():
    result = evaluate_alpha_follow_through_exit(
        enabled=True,
        bars_held=5,
        mfe_r=0.10,
        mae_r=0.20,
        tp1_filled=True,
        follow_through_bars=3,
        follow_through_min_mfe_r=0.35,
        early_exit_max_mae_r=0.75,
    )

    assert result.should_exit is False
    assert result.reason == "tp1 already filled"


def test_alpha_follow_through_does_not_market_exit_profitable_stall_before_tp():
    result = evaluate_alpha_follow_through_exit(
        enabled=True,
        bars_held=3,
        mfe_r=0.31,
        mae_r=0.20,
        tp1_filled=False,
        follow_through_bars=3,
        follow_through_min_mfe_r=0.35,
        early_exit_max_mae_r=0.75,
        current_r=0.18,
        stall_exit_max_current_r=0.0,
    )

    assert result.should_exit is False
    assert "hold profitable stall" in result.reason

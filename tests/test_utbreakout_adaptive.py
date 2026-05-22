from utbreakout.adaptive import (
    build_adaptive_exit_overlay,
    build_dynamic_chandelier_stop,
    build_strategy_adaptation,
    build_trend_health_score,
    evaluate_shadow_runner_exit,
    evaluate_shadow_triple_barrier,
    summarize_runner_outcomes,
    summarize_shadow_outcomes,
)


def test_shadow_triple_barrier_records_take_profit_first():
    result = evaluate_shadow_triple_barrier(
        side="long",
        entry_price=100,
        stop_loss=95,
        take_profit=110,
        decision_ts=1000,
        bars=[
            {"timestamp": 1000, "high": 101, "low": 99, "close": 100},
            {"timestamp": 2000, "high": 106, "low": 99, "close": 105},
            {"timestamp": 3000, "high": 111, "low": 104, "close": 110},
        ],
        max_bars=5,
    )

    assert result["outcome"] == "tp"
    assert result["code"] == "SHADOW_TP"
    assert result["pnl_r"] == 2.0
    assert result["bars_elapsed"] == 2
    assert result["mfe_r"] >= 2.0


def test_shadow_triple_barrier_is_conservative_when_tp_and_sl_hit_same_bar():
    result = evaluate_shadow_triple_barrier(
        side="short",
        entry_price=100,
        stop_loss=105,
        take_profit=90,
        decision_ts=1000,
        bars=[
            {"timestamp": 2000, "high": 106, "low": 89, "close": 94},
        ],
        max_bars=3,
    )

    assert result["outcome"] == "sl"
    assert result["pnl_r"] == -1.0


def test_shadow_triple_barrier_waits_until_time_barrier_is_complete():
    result = evaluate_shadow_triple_barrier(
        side="long",
        entry_price=100,
        stop_loss=95,
        take_profit=110,
        decision_ts=1000,
        bars=[
            {"timestamp": 2000, "high": 103, "low": 98, "close": 102},
        ],
        max_bars=2,
    )

    assert result is None


def test_adaptive_strategy_reduces_risk_from_high_vol_and_weak_shadow_edge():
    events = [
        {"event": "shadow_outcome", "symbol": "BTC/USDT", "side": "long", "shadow_outcome": "sl", "pnl_r": -1.0, "mfe_r": 0.4, "mae_r": 1.1},
        {"event": "shadow_outcome", "symbol": "BTC/USDT", "side": "long", "shadow_outcome": "sl", "pnl_r": -1.0, "mfe_r": 0.7, "mae_r": 1.2},
        {"event": "shadow_outcome", "symbol": "BTC/USDT", "side": "long", "shadow_outcome": "timeout", "pnl_r": -0.2, "mfe_r": 1.0, "mae_r": 0.8},
        {"event": "shadow_outcome", "symbol": "BTC/USDT", "side": "long", "shadow_outcome": "tp", "pnl_r": 2.0, "mfe_r": 2.0, "mae_r": 0.3},
    ]
    stats = summarize_shadow_outcomes(events, symbol="BTC/USDT", side="long")

    result = build_strategy_adaptation(
        {
            "volatility_targeting_enabled": True,
            "volatility_target_atr_pct": 0.75,
            "volatility_target_min_multiplier": 0.25,
            "meta_labeling_enabled": True,
            "meta_labeling_min_samples": 4,
            "meta_labeling_min_multiplier": 0.5,
            "strategy_adaptive_min_risk_multiplier": 0.25,
            "adaptive_exit_min_samples": 4,
        },
        stats,
        side="long",
        atr_pct=1.5,
    )

    assert result["volatility_risk_multiplier"] == 0.5
    assert result["meta_label_risk_multiplier"] < 1
    assert result["risk_multiplier"] <= 0.375


def test_adaptive_exit_overlay_makes_short_exits_more_defensive():
    overlay = build_adaptive_exit_overlay(
        {
            "partial_take_profit_r_multiple": 1.5,
            "partial_take_profit_ratio": 0.5,
            "atr_trailing_multiplier": 2.0,
            "atr_trailing_activation_r": 1.5,
            "short_asymmetry_enabled": True,
            "short_partial_take_profit_r_delta": 0.2,
            "short_partial_take_profit_ratio_add": 0.1,
            "short_atr_trailing_multiplier_delta": 0.25,
            "short_atr_trailing_activation_r_delta": 0.2,
        },
        {"sample_count": 0},
        "short",
    )

    assert overlay["partial_take_profit_r_multiple"] == 1.3
    assert overlay["partial_take_profit_ratio"] == 0.6
    assert overlay["atr_trailing_multiplier"] == 1.75
    assert overlay["atr_trailing_activation_r"] == 1.3


def test_trend_health_compresses_quality_into_single_multiplier():
    strong = build_trend_health_score(
        {
            "trend_health_enabled": True,
            "trend_health_hard_block_below": 40,
            "trend_health_reduce_below": 55,
            "trend_health_full_score": 75,
            "trend_health_min_multiplier": 0.35,
        },
        {
            "directional_efficiency": 0.5,
            "volatility_expansion_ratio": 1.4,
            "entry_price": 110,
            "atr": 2,
            "donchian_high_prev": 108,
            "keltner_upper": 107,
            "htf_close": 110,
            "htf_ema_fast": 105,
            "htf_ema_slow": 100,
            "htf_supertrend_direction": "long",
        },
        "long",
    )
    weak = build_trend_health_score(
        {
            "trend_health_enabled": True,
            "trend_health_hard_block_below": 40,
            "trend_health_reduce_below": 55,
            "trend_health_min_multiplier": 0.35,
        },
        {
            "directional_efficiency": 0.05,
            "volatility_expansion_ratio": 0.5,
            "entry_price": 100,
            "atr": 2,
            "donchian_high_prev": 105,
            "htf_close": 100,
            "htf_ema_fast": 99,
            "htf_ema_slow": 101,
        },
        "long",
    )

    assert strong["state"] is True
    assert strong["risk_multiplier"] == 1.0
    assert weak["state"] is False
    assert weak["risk_multiplier"] == 0.0


def test_dynamic_chandelier_stop_uses_extreme_and_structure_for_runner():
    stop = build_dynamic_chandelier_stop(
        side="long",
        current_stop=100,
        entry_price=100,
        current_close=122,
        atr_value=2,
        highest_high=126,
        recent_swing_low=119,
        risk_distance=10,
        trend_health={"score": 80},
        cfg={
            "runner_chandelier_multiplier": 2.0,
            "runner_chandelier_multiplier_min": 1.4,
            "runner_chandelier_multiplier_max": 3.2,
            "runner_structure_buffer_atr": 0.2,
            "runner_dynamic_multiplier_enabled": False,
            "atr_trailing_breakeven_enabled": True,
        },
    )

    assert stop["mode"] == "dynamic_chandelier"
    assert stop["stop_price"] > 118
    assert stop["stop_price"] < 122


def test_shadow_runner_exit_tracks_mfe_capture_after_partial_profit():
    bars = [{"timestamp": 1000, "open": 100, "high": 101, "low": 99, "close": 100}]
    close = 100.0
    for idx in range(1, 65):
        close += 0.7 if idx < 45 else -0.45
        bars.append({
            "timestamp": 1000 + idx * 1000,
            "open": close - 0.2,
            "high": close + 0.8,
            "low": close - 0.8,
            "close": close,
        })

    result = evaluate_shadow_runner_exit(
        side="long",
        entry_price=100,
        stop_loss=95,
        risk_distance=5,
        decision_ts=1000,
        bars=bars,
        cfg={
            "atr_length": 3,
            "partial_take_profit_r_multiple": 1.0,
            "partial_take_profit_ratio": 0.35,
            "atr_trailing_activation_r": 1.0,
            "runner_chandelier_lookback": 10,
            "runner_structure_lookback": 3,
            "runner_chandelier_multiplier": 2.0,
            "runner_chandelier_multiplier_min": 1.4,
            "runner_chandelier_multiplier_max": 3.0,
            "runner_dynamic_multiplier_enabled": False,
            "shadow_runner_max_bars": 48,
        },
        max_bars=48,
    )

    assert result["partial_filled"] is True
    assert result["mfe_r"] > 4
    assert result["pnl_r"] > 1
    assert 0 < result["mfe_capture_ratio"] <= 1


def test_strategy_adaptation_uses_trend_health_and_runner_capture():
    runner_stats = summarize_runner_outcomes([
        {"event": "runner_shadow_outcome", "symbol": "BTC/USDT", "side": "long", "runner_outcome": "runner_stop", "pnl_r": 0.7, "mfe_r": 2.5, "mfe_capture_ratio": 0.28},
        {"event": "runner_shadow_outcome", "symbol": "BTC/USDT", "side": "long", "runner_outcome": "runner_stop", "pnl_r": 0.8, "mfe_r": 2.2, "mfe_capture_ratio": 0.36},
    ], symbol="BTC/USDT", side="long")
    result = build_strategy_adaptation(
        {
            "volatility_targeting_enabled": True,
            "volatility_target_atr_pct": 1.0,
            "volatility_target_min_multiplier": 0.25,
            "meta_labeling_enabled": False,
            "strategy_adaptive_min_risk_multiplier": 0.25,
            "adaptive_exit_min_samples": 2,
            "runner_exit_enabled": True,
            "partial_take_profit_r_multiple": 1.5,
            "partial_take_profit_ratio": 0.5,
            "atr_trailing_multiplier": 2.0,
            "atr_trailing_activation_r": 1.5,
        },
        {"sample_count": 0},
        side="long",
        atr_pct=1.0,
        runner_stats=runner_stats,
        trend_health={"score": 50, "state": "reduced", "risk_multiplier": 0.5, "summary": "trend health REDUCE"},
    )

    assert result["trend_health_risk_multiplier"] == 0.5
    assert result["risk_multiplier"] == 0.5
    assert result["exit_overlay"]["atr_trailing_multiplier"] < 2.0

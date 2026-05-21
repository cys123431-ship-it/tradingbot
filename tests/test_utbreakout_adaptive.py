from utbreakout.adaptive import (
    build_adaptive_exit_overlay,
    build_strategy_adaptation,
    evaluate_shadow_triple_barrier,
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

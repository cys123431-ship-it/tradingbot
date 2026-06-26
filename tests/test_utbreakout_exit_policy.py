from utbreakout.exit_policy import (
    EXIT_POLICY_CANDIDATES,
    PolicyStatsStore,
    build_default_exit_policy,
    build_exit_policy,
    build_volatility_adaptive_tp,
    choose_exit_policy,
    evaluate_signal_invalid_exit,
    evaluate_time_stop,
    rank_exit_policies,
)


def test_default_exit_policy_runner_disabled_and_uses_tp1_tp2_be():
    policy = build_default_exit_policy({})

    assert policy.runner_enabled is False
    assert policy.tp1_r > 0
    assert policy.tp2_r > policy.tp1_r
    assert policy.move_sl_to_be_after_tp1 is True


def test_time_stop_exits_if_tp1_not_hit_within_max_bars():
    signal = evaluate_time_stop(
        {"entry_bar_index": 10, "timeframe": "15m", "tp1_filled": False},
        {"index": 18},
        {},
    )

    assert signal.should_exit is True
    assert signal.reason == "TIME_STOP_NO_FOLLOW_THROUGH"


def test_time_stop_does_not_exit_after_tp1_filled():
    signal = evaluate_time_stop(
        {"entry_bar_index": 10, "timeframe": "15m", "tp1_filled": True},
        {"index": 40},
        {},
    )

    assert signal.should_exit is False


def test_signal_invalid_exit_detects_long_and_short_reversals():
    long_exit = evaluate_signal_invalid_exit(
        {"side": "long"},
        {"utbot_direction": "SHORT", "plus_di": 10, "minus_di": 20, "close": 99, "donchian_mid": 100, "htf_trend": "DOWN"},
    )
    short_exit = evaluate_signal_invalid_exit(
        {"side": "short"},
        {"utbot_direction": "LONG", "plus_di": 22, "minus_di": 8, "close": 101, "donchian_mid": 100, "htf_trend": "UP"},
    )

    assert long_exit.should_exit is True
    assert "UTBOT_REVERSAL" in long_exit.reason
    assert short_exit.should_exit is True
    assert "DMI_REVERSAL" in short_exit.reason


def test_signal_invalid_exit_can_be_disabled():
    signal = evaluate_signal_invalid_exit(
        {"side": "long"},
        {"utbot_direction": "SHORT"},
        {"signal_invalid_exit_enabled": False},
    )

    assert signal.should_exit is False


def test_volatility_adaptive_tp_shortens_low_vol_and_reduces_high_vol_risk():
    low = build_volatility_adaptive_tp({"atr_percentile": 10})
    normal = build_volatility_adaptive_tp({"atr_percentile": 50})
    high = build_volatility_adaptive_tp({"atr_percentile": 90})

    assert low.tp1_r < normal.tp1_r
    assert low.risk_multiplier < 1
    assert high.risk_multiplier < 1
    assert high.tp2_r > high.tp1_r


def test_exit_policy_selector_defaults_to_hybrid_and_excludes_runner():
    policy = choose_exit_policy(
        {"side": "long"},
        {"symbol": "BTC/USDT", "timeframe": "15m", "regime": "TREND_UP"},
        PolicyStatsStore({}),
    )

    assert policy.name == "HYBRID_DEFENSIVE_FIXED_TP"
    assert "RUNNER" not in EXIT_POLICY_CANDIDATES


def test_exit_policy_selector_prefers_risk_adjusted_not_raw_return():
    stats = PolicyStatsStore({
        "FIXED_TP": {"trades": 50, "expectancy_r": 0.7, "oos_expectancy_r": 0.7, "profit_factor": 1.5, "max_drawdown_pct": 25},
        "FIXED_TP_TIME_STOP": {"trades": 50, "expectancy_r": 0.45, "oos_expectancy_r": 0.45, "profit_factor": 1.4, "max_drawdown_pct": 5},
    })

    policy = choose_exit_policy({"side": "long"}, {"regime": "TREND_UP"}, stats, {"max_policy_drawdown_pct": 15})

    assert policy.name == "FIXED_TP_TIME_STOP"


def test_rank_exit_policies_rejects_negative_expectancy_and_low_trade_count():
    ranked = rank_exit_policies(
        {
            "bad": {"trades": 100, "expectancy_r": -0.1, "profit_factor": 2.0},
            "thin": {"trades": 2, "expectancy_r": 1.0, "profit_factor": 2.0},
            "good": {"trades": 100, "expectancy_r": 0.3, "profit_factor": 1.2},
        },
        {"min_policy_trade_count": 30},
    )

    assert [item[1] for item in ranked] == ["good"]


def test_build_named_policy_flags():
    policy = build_exit_policy("FIXED_TP_TIME_STOP")

    assert policy.time_stop_enabled is True
    assert policy.signal_invalid_exit_enabled is False
    assert policy.runner_enabled is False

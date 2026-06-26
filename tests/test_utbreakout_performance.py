from math import isinf

from utbreakout.performance import (
    apply_multiple_testing_penalty,
    calculate_performance_metrics,
    deflated_sharpe_proxy,
    group_performance,
    passes_oos_rules,
    pbo_proxy,
    walk_forward_splits,
)


def test_performance_metrics_include_risk_and_capture_stats():
    trades = [
        {"pnl_r": 2.0, "pnl_usdt": 20, "return_pct": 1.0, "mfe_r": 2.4, "mae_r": 0.3, "mfe_capture_ratio": 0.8, "side": "long"},
        {"pnl_r": -1.0, "pnl_usdt": -10, "return_pct": -0.5, "mfe_r": 0.4, "mae_r": 1.0, "mfe_capture_ratio": 0.0, "side": "short"},
        {"pnl_r": 1.0, "pnl_usdt": 8, "return_pct": 0.4, "mfe_r": 1.4, "mae_r": 0.4, "mfe_capture_ratio": 0.7, "side": "long"},
    ]

    result = calculate_performance_metrics(trades)

    assert result["trade_count"] == 3
    assert result["total_pnl_usdt"] == 18
    assert abs(result["win_rate"] - 2 / 3) < 1e-6
    assert result["profit_factor"] == 2.8
    assert result["max_drawdown_usdt"] == 10
    assert result["avg_mfe_capture_ratio"] == 0.5


def test_performance_profit_factor_is_infinite_without_losses():
    result = calculate_performance_metrics([{"pnl_r": 1, "pnl_usdt": 5}, {"pnl_r": 0.5, "pnl_usdt": 2}])

    assert isinf(result["profit_factor"])


def test_performance_groups_by_side_and_builds_walk_forward_splits():
    trades = [{"pnl_r": 1 if idx % 2 == 0 else -0.5, "pnl_usdt": 10 if idx % 2 == 0 else -5, "side": "long" if idx < 6 else "short"} for idx in range(12)]

    grouped = group_performance(trades, key="side")
    splits = walk_forward_splits(trades, train_size=4, test_size=2)

    assert set(grouped) == {"long", "short"}
    assert len(splits) == 4
    assert splits[0]["train"]["trade_count"] == 4
    assert splits[0]["test"]["trade_count"] == 2


def test_multiple_testing_penalty_increases_with_trials():
    low_trials = apply_multiple_testing_penalty({"expectancy_r": 1.0, "profit_factor": 2.0}, 5)
    many_trials = apply_multiple_testing_penalty({"expectancy_r": 1.0, "profit_factor": 2.0}, 100)

    assert low_trials["multiple_testing_penalty"] == 0.0
    assert many_trials["multiple_testing_penalty"] > low_trials["multiple_testing_penalty"]
    assert many_trials["adjusted_expectancy_r"] < 1.0


def test_deflated_sharpe_proxy_rejects_low_sharpe_many_trials():
    assert deflated_sharpe_proxy(0.4, n_obs=80, n_trials=100) is False
    assert deflated_sharpe_proxy(1.2, n_obs=300, n_trials=5) is True


def test_oos_failure_and_pbo_proxy_reject_strategy():
    assert passes_oos_rules({"trades": 3, "average_R": 1.0, "profit_factor": 2.0}) is False
    assert pbo_proxy([True, False, False, True]) == 0.5

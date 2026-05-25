from math import isinf

from utbreakout.performance import calculate_performance_metrics, group_performance, walk_forward_splits


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

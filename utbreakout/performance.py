"""Backtest/research performance metrics for UT Breakout."""

from collections import defaultdict
from math import isfinite, log, sqrt


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _avg(values):
    values = [_finite_float(v, None) for v in values]
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else 0.0


def _std(values):
    values = [_finite_float(v, None) for v in values]
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _max_drawdown(equity_curve):
    peak = 0.0
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    return abs(max_dd)


def calculate_performance_metrics(trades=None, *, periods_per_year=365):
    trades = [dict(trade or {}) for trade in (trades or [])]
    pnl_r = [_finite_float(t.get("pnl_r"), 0.0) for t in trades]
    pnl_usdt = [_finite_float(t.get("pnl_usdt"), _finite_float(t.get("pnl"), 0.0)) for t in trades]
    wins = [value for value in pnl_r if value > 0]
    losses = [value for value in pnl_r if value < 0]
    gross_profit = sum(value for value in pnl_usdt if value > 0)
    gross_loss = abs(sum(value for value in pnl_usdt if value < 0))
    equity = []
    running = 0.0
    for value in pnl_usdt:
        running += value
        equity.append(running)
    avg_return = _avg(t.get("return_pct", t.get("pnl_r", 0.0)) for t in trades)
    volatility = _std(t.get("return_pct", t.get("pnl_r", 0.0)) for t in trades)
    downside = _std([min(0.0, _finite_float(t.get("return_pct", t.get("pnl_r", 0.0)), 0.0)) for t in trades])
    max_dd = _max_drawdown(equity)
    total_pnl = sum(pnl_usdt)
    count = len(trades)
    return {
        "trade_count": count,
        "total_pnl_usdt": round(total_pnl, 8),
        "cumulative_pnl_usdt": round(total_pnl, 8),
        "avg_r": round(_avg(pnl_r), 8),
        "win_rate": round(len(wins) / count if count else 0.0, 8),
        "avg_win_r": round(_avg(wins), 8),
        "avg_loss_r": round(_avg(losses), 8),
        "avg_win_loss_ratio": round((_avg(wins) / abs(_avg(losses))) if losses and _avg(losses) else 0.0, 8),
        "profit_factor": round((gross_profit / gross_loss) if gross_loss else (float("inf") if gross_profit > 0 else 0.0), 8),
        "max_drawdown_usdt": round(max_dd, 8),
        "sharpe_ratio": round((avg_return / volatility * sqrt(periods_per_year)) if volatility else 0.0, 8),
        "sortino_ratio": round((avg_return / downside * sqrt(periods_per_year)) if downside else 0.0, 8),
        "calmar_ratio": round((total_pnl / max_dd) if max_dd else 0.0, 8),
        "avg_mfe_r": round(_avg(t.get("mfe_r") for t in trades), 8),
        "avg_mae_r": round(_avg(t.get("mae_r") for t in trades), 8),
        "avg_mfe_capture_ratio": round(_avg(t.get("mfe_capture_ratio") for t in trades), 8),
    }


def group_performance(trades=None, key="side"):
    groups = defaultdict(list)
    for trade in trades or []:
        groups[str((trade or {}).get(key) or "unknown")].append(trade)
    return {name: calculate_performance_metrics(rows) for name, rows in groups.items()}


def walk_forward_splits(trades=None, train_size=50, test_size=20):
    trades = list(trades or [])
    train_size = max(1, int(train_size or 1))
    test_size = max(1, int(test_size or 1))
    splits = []
    start = 0
    while start + train_size + test_size <= len(trades):
        train = trades[start:start + train_size]
        test = trades[start + train_size:start + train_size + test_size]
        splits.append({
            "train_start": start,
            "train_end": start + train_size,
            "test_start": start + train_size,
            "test_end": start + train_size + test_size,
            "train": calculate_performance_metrics(train),
            "test": calculate_performance_metrics(test),
        })
        start += test_size
    return splits


def passes_train_rules(report, config=None):
    config = dict(config or {})
    report = dict(report or {})
    if int(_finite_float(report.get("total_trades", report.get("trades")), 0)) < int(config.get("min_train_trades", 30) or 30):
        return False
    if _finite_float(report.get("profit_factor"), 0.0) < _finite_float(config.get("min_train_profit_factor"), 1.15):
        return False
    if _finite_float(report.get("expectancy_r", report.get("average_R")), 0.0) <= 0:
        return False
    if _finite_float(report.get("max_drawdown_pct"), 0.0) > _finite_float(config.get("max_train_drawdown_pct"), 15.0):
        return False
    return True


def passes_oos_rules(report, config=None):
    config = dict(config or {})
    report = dict(report or {})
    if int(_finite_float(report.get("total_trades", report.get("trades")), 0)) < int(config.get("min_oos_trades", 10) or 10):
        return False
    if _finite_float(report.get("expectancy_r", report.get("average_R")), 0.0) <= 0:
        return False
    if _finite_float(report.get("profit_factor"), 0.0) < _finite_float(config.get("min_oos_profit_factor"), 1.05):
        return False
    if _finite_float(report.get("max_drawdown_pct"), 0.0) > _finite_float(config.get("max_oos_drawdown_pct"), 20.0):
        return False
    return True


def apply_multiple_testing_penalty(report, number_of_trials, config=None):
    report = dict(report or {})
    trials = max(1, int(number_of_trials or 1))
    if trials <= int((config or {}).get("multiple_testing_free_trials", 10) or 10):
        report.setdefault("adjusted_expectancy_r", _finite_float(report.get("expectancy_r", report.get("average_R")), 0.0))
        report.setdefault("adjusted_profit_factor", _finite_float(report.get("profit_factor"), 0.0))
        report["multiple_testing_penalty"] = 0.0
        return report
    penalty = min(_finite_float((config or {}).get("multiple_testing_max_penalty"), 0.30), 0.02 * log(trials))
    expectancy = _finite_float(report.get("expectancy_r", report.get("average_R")), 0.0)
    pf = _finite_float(report.get("profit_factor"), 0.0)
    report["multiple_testing_penalty"] = round(penalty, 8)
    report["adjusted_expectancy_r"] = expectancy * (1.0 - penalty)
    report["adjusted_profit_factor"] = 1.0 + (pf - 1.0) * (1.0 - penalty)
    return report


def deflated_sharpe_proxy(sharpe, skew=0.0, kurtosis=3.0, n_obs=0, n_trials=1):
    """Proxy threshold for deflated Sharpe style selection.

    More trials and shorter/noisier samples raise the required Sharpe.
    """
    required = 0.5 + 0.05 * log(max(int(n_trials or 1), 1))
    if int(n_obs or 0) < 100:
        required += 0.25
    if _finite_float(kurtosis, 3.0) > 3:
        required += 0.10
    if abs(_finite_float(skew, 0.0)) > 1.0:
        required += 0.05
    return _finite_float(sharpe, 0.0) > required


def pbo_proxy(oos_passes=None):
    values = list(oos_passes or [])
    if not values:
        return 1.0
    failures = sum(1 for value in values if not bool(value))
    return failures / len(values)

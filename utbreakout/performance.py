"""Canonical backtest and live-trade performance metrics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
from math import isfinite, log, sqrt
from typing import Any


logger = logging.getLogger(__name__)


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _avg(values):
    values = [_finite_float(value, None) for value in values]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else 0.0


def _std(values):
    values = [_finite_float(value, None) for value in values]
    values = [value for value in values if value is not None]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _max_drawdown(equity_curve):
    if not equity_curve:
        return 0.0
    peak = float(equity_curve[0])
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    return max_drawdown


def _timestamp_day(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def _daily_returns(trades, initial_equity_usdt):
    if not trades or not initial_equity_usdt or initial_equity_usdt <= 0:
        return []
    by_day = defaultdict(float)
    for trade in trades:
        day = _timestamp_day(trade.get("exit_time") or trade.get("timestamp") or trade.get("exit_ts"))
        if day is None:
            return []
        by_day[day] += _finite_float(trade.get("pnl_usdt", trade.get("pnl")), 0.0)
    first = datetime.fromisoformat(min(by_day)).date()
    last = datetime.fromisoformat(max(by_day)).date()
    equity = float(initial_equity_usdt)
    returns = []
    day = first
    while day <= last:
        pnl = by_day.get(day.isoformat(), 0.0)
        returns.append(pnl / max(equity, 1e-9))
        equity += pnl
        day = day.fromordinal(day.toordinal() + 1)
    return returns


@dataclass(frozen=True)
class PerformanceMetrics(Mapping[str, Any]):
    trade_count: int
    expectancy_r: float
    profit_factor: float
    max_drawdown_usdt: float
    max_drawdown_pct: float | None
    win_rate: float
    net_profit_usdt: float
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    avg_win_loss_ratio: float = 0.0
    trade_return_sharpe_unannualized: float = 0.0
    daily_sharpe_annualized: float | None = None
    sortino_ratio_unannualized: float = 0.0
    calmar_ratio: float | None = None
    avg_mfe_r: float = 0.0
    avg_mae_r: float = 0.0
    avg_mfe_capture_ratio: float = 0.0
    sharpe_basis: str = "trade_return_unannualized"

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values.update(
            {
                "total_pnl_usdt": self.net_profit_usdt,
                "cumulative_pnl_usdt": self.net_profit_usdt,
                "avg_r": self.expectancy_r,
                "sharpe_ratio": (
                    self.daily_sharpe_annualized
                    if self.daily_sharpe_annualized is not None
                    else self.trade_return_sharpe_unannualized
                ),
                "sortino_ratio": self.sortino_ratio_unannualized,
            }
        )
        return values

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    @classmethod
    def from_mapping(cls, report: Mapping[str, Any]) -> "PerformanceMetrics":
        required = (
            "trade_count",
            "expectancy_r",
            "profit_factor",
            "max_drawdown_usdt",
            "max_drawdown_pct",
            "win_rate",
            "net_profit_usdt",
        )
        missing = [key for key in required if key not in report or report.get(key) is None]
        if missing:
            raise ValueError(f"missing required performance metrics: {', '.join(missing)}")
        return cls(
            trade_count=int(report["trade_count"]),
            expectancy_r=float(report["expectancy_r"]),
            profit_factor=float(report["profit_factor"]),
            max_drawdown_usdt=float(report["max_drawdown_usdt"]),
            max_drawdown_pct=float(report["max_drawdown_pct"]),
            win_rate=float(report["win_rate"]),
            net_profit_usdt=float(report["net_profit_usdt"]),
            avg_win_r=_finite_float(report.get("avg_win_r"), 0.0),
            avg_loss_r=_finite_float(report.get("avg_loss_r"), 0.0),
            avg_win_loss_ratio=_finite_float(report.get("avg_win_loss_ratio"), 0.0),
            trade_return_sharpe_unannualized=_finite_float(
                report.get("trade_return_sharpe_unannualized"), 0.0
            ),
            daily_sharpe_annualized=_finite_float(report.get("daily_sharpe_annualized"), None),
            sortino_ratio_unannualized=_finite_float(report.get("sortino_ratio_unannualized"), 0.0),
            calmar_ratio=_finite_float(report.get("calmar_ratio"), None),
            avg_mfe_r=_finite_float(report.get("avg_mfe_r"), 0.0),
            avg_mae_r=_finite_float(report.get("avg_mae_r"), 0.0),
            avg_mfe_capture_ratio=_finite_float(report.get("avg_mfe_capture_ratio"), 0.0),
            sharpe_basis=str(report.get("sharpe_basis") or "trade_return_unannualized"),
        )


def calculate_performance_metrics(trades=None, *, initial_equity_usdt=None, periods_per_year=365):
    trades = [dict(trade or {}) for trade in (trades or [])]
    pnl_r = [_finite_float(trade.get("pnl_r", trade.get("r_multiple")), 0.0) for trade in trades]
    pnl_usdt = [
        _finite_float(trade.get("pnl_usdt"), _finite_float(trade.get("pnl"), 0.0))
        for trade in trades
    ]
    wins = [value for value in pnl_r if value > 0]
    losses = [value for value in pnl_r if value < 0]
    gross_profit = sum(value for value in pnl_usdt if value > 0)
    gross_loss = abs(sum(value for value in pnl_usdt if value < 0))
    start = float(initial_equity_usdt or 0.0)
    running = start
    equity = [running]
    for value in pnl_usdt:
        running += value
        equity.append(running)
    returns = [
        _finite_float(trade.get("return_pct", trade.get("pnl_r", trade.get("r_multiple"))), 0.0)
        for trade in trades
    ]
    avg_return = _avg(returns)
    volatility = _std(returns)
    downside = _std([min(0.0, value) for value in returns])
    daily_returns = _daily_returns(trades, start)
    daily_std = _std(daily_returns)
    daily_sharpe = (
        _avg(daily_returns) / daily_std * sqrt(periods_per_year)
        if len(daily_returns) >= 2 and daily_std
        else None
    )
    max_drawdown = _max_drawdown(equity)
    max_drawdown_pct = (
        max_drawdown / start * 100.0 if start > 0 else None
    )
    total_pnl = sum(pnl_usdt)
    count = len(trades)
    return PerformanceMetrics(
        trade_count=count,
        expectancy_r=round(_avg(pnl_r), 8),
        profit_factor=round(
            (gross_profit / gross_loss) if gross_loss else (float("inf") if gross_profit > 0 else 0.0),
            8,
        ),
        max_drawdown_usdt=round(max_drawdown, 8),
        max_drawdown_pct=round(max_drawdown_pct, 8) if max_drawdown_pct is not None else None,
        win_rate=round(len(wins) / count if count else 0.0, 8),
        net_profit_usdt=round(total_pnl, 8),
        avg_win_r=round(_avg(wins), 8),
        avg_loss_r=round(_avg(losses), 8),
        avg_win_loss_ratio=round((_avg(wins) / abs(_avg(losses))) if losses and _avg(losses) else 0.0, 8),
        trade_return_sharpe_unannualized=round((avg_return / volatility) if volatility else 0.0, 8),
        daily_sharpe_annualized=round(daily_sharpe, 8) if daily_sharpe is not None else None,
        sortino_ratio_unannualized=round((avg_return / downside) if downside else 0.0, 8),
        calmar_ratio=round((total_pnl / max_drawdown) if max_drawdown else 0.0, 8),
        avg_mfe_r=round(_avg(trade.get("mfe_r") for trade in trades), 8),
        avg_mae_r=round(_avg(trade.get("mae_r") for trade in trades), 8),
        avg_mfe_capture_ratio=round(_avg(trade.get("mfe_capture_ratio") for trade in trades), 8),
        sharpe_basis="daily_annualized" if daily_sharpe is not None else "trade_return_unannualized",
    )


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
        splits.append(
            {
                "train_start": start,
                "train_end": start + train_size,
                "test_start": start + train_size,
                "test_end": start + train_size + test_size,
                "train": calculate_performance_metrics(train),
                "test": calculate_performance_metrics(test),
            }
        )
        start += test_size
    return splits


def _strict_metrics(report):
    try:
        metrics = report if isinstance(report, PerformanceMetrics) else PerformanceMetrics.from_mapping(report or {})
        if metrics.max_drawdown_pct is None:
            raise ValueError("max_drawdown_pct is required for approval")
        return metrics
    except (TypeError, ValueError):
        logger.exception("Performance validation failed because canonical metrics were missing")
        return None


def passes_train_rules(report, config=None):
    config = dict(config or {})
    metrics = _strict_metrics(report)
    if metrics is None:
        return False
    if metrics.trade_count < int(config.get("min_train_trades", 30) or 30):
        return False
    if metrics.profit_factor < _finite_float(config.get("min_train_profit_factor"), 1.15):
        return False
    if metrics.expectancy_r <= 0:
        return False
    if metrics.max_drawdown_pct > _finite_float(config.get("max_train_drawdown_pct"), 15.0):
        return False
    return True


def passes_oos_rules(report, config=None):
    config = dict(config or {})
    metrics = _strict_metrics(report)
    if metrics is None:
        return False
    if metrics.trade_count < int(config.get("min_oos_trades", 10) or 10):
        return False
    if metrics.expectancy_r <= 0:
        return False
    if metrics.profit_factor < _finite_float(config.get("min_oos_profit_factor"), 1.05):
        return False
    if metrics.max_drawdown_pct > _finite_float(config.get("max_oos_drawdown_pct"), 20.0):
        return False
    return True


def apply_multiple_testing_penalty(report, number_of_trials, config=None):
    report = dict(report or {})
    trials = max(1, int(number_of_trials or 1))
    if trials <= int((config or {}).get("multiple_testing_free_trials", 10) or 10):
        report.setdefault("adjusted_expectancy_r", _finite_float(report.get("expectancy_r"), 0.0))
        report.setdefault("adjusted_profit_factor", _finite_float(report.get("profit_factor"), 0.0))
        report["multiple_testing_penalty"] = 0.0
        return report
    penalty = min(_finite_float((config or {}).get("multiple_testing_max_penalty"), 0.30), 0.02 * log(trials))
    expectancy = _finite_float(report.get("expectancy_r"), 0.0)
    profit_factor = _finite_float(report.get("profit_factor"), 0.0)
    report["multiple_testing_penalty"] = round(penalty, 8)
    report["adjusted_expectancy_r"] = expectancy * (1.0 - penalty)
    report["adjusted_profit_factor"] = 1.0 + (profit_factor - 1.0) * (1.0 - penalty)
    return report


def deflated_sharpe_proxy(sharpe, skew=0.0, kurtosis=3.0, n_obs=0, n_trials=1):
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

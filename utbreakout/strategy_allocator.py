"""Adaptive strategy risk allocator based on finalized live-trade outcomes.

The allocator only scales risk; it never changes direction, entry, stop or take
profit. Small samples remain at neutral risk, while sufficiently poor recent
results reduce allocation without silently disabling a strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Iterable, Mapping


def default_strategy_allocator_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "lookback_trades": 30,
        "minimum_samples": 8,
        "full_confidence_samples": 20,
        "risk_floor": 0.30,
        "risk_cap": 1.00,
        "negative_expectancy_multiplier": 0.60,
        "weak_profit_factor_multiplier": 0.75,
        "loss_streak_multiplier": 0.70,
        "drawdown_multiplier": 0.75,
        "poor_capture_multiplier": 0.80,
        "loss_streak_threshold": 4,
        "drawdown_r_threshold": 4.0,
        "mfe_capture_min": 0.35,
        "good_expectancy_r": 0.12,
        "good_profit_factor": 1.20,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row, Mapping) else None
    return dict(payload) if isinstance(payload, Mapping) else dict(row or {})


def _strategy(row: Mapping[str, Any]) -> str:
    payload = _payload(row)
    return str(
        payload.get("strategy")
        or payload.get("entry_strategy")
        or payload.get("engine")
        or row.get("strategy")
        or "unknown"
    ).strip().lower()


def _net_pnl(row: Mapping[str, Any]) -> float:
    payload = _payload(row)
    for key in ("net_pnl", "net_pnl_usdt", "realized_pnl", "pnl", "gross_pnl"):
        value = _finite(payload.get(key, row.get(key)))
        if value is not None:
            return value
    return 0.0


def _risk_usdt(row: Mapping[str, Any]) -> float | None:
    payload = _payload(row)
    for key in ("initial_risk_usdt", "risk_usdt", "planned_risk_usdt", "max_risk_per_trade_usdt"):
        value = _finite(payload.get(key, row.get(key)))
        if value is not None and value > 0:
            return value
    return None


def _r_multiple(row: Mapping[str, Any]) -> float | None:
    payload = _payload(row)
    for key in ("net_r", "realized_r", "r_multiple", "pnl_r"):
        value = _finite(payload.get(key, row.get(key)))
        if value is not None:
            return value
    risk = _risk_usdt(row)
    return _net_pnl(row) / risk if risk and risk > 0 else None


def _mfe_r(row: Mapping[str, Any]) -> float | None:
    payload = _payload(row)
    for key in ("mfe_r", "maximum_favorable_excursion_r", "max_favorable_excursion_r"):
        value = _finite(payload.get(key, row.get(key)))
        if value is not None:
            return value
    mfe_usdt = _finite(payload.get("mfe_usdt", row.get("mfe_usdt")))
    risk = _risk_usdt(row)
    return mfe_usdt / risk if mfe_usdt is not None and risk and risk > 0 else None


def summarize_strategy_trades(
    trades: Iterable[Mapping[str, Any]] | None,
    strategy: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = {**default_strategy_allocator_config(), **dict(config or {})}
    wanted = str(strategy or "").strip().lower()
    rows = [dict(row) for row in trades or [] if isinstance(row, Mapping) and _strategy(row) == wanted]
    rows = rows[-max(1, int(cfg["lookback_trades"])):]
    values = [value for value in (_r_multiple(row) for row in rows) if value is not None]
    pnl_values = [_net_pnl(row) for row in rows]
    wins = sum(value for value in pnl_values if value > 0)
    losses = abs(sum(value for value in pnl_values if value < 0))
    profit_factor = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
    expectancy_r = sum(values) / len(values) if values else 0.0
    equity = peak = drawdown = 0.0
    loss_streak = max_loss_streak = 0
    captures = []
    for row, value in zip(rows, [_r_multiple(row) for row in rows]):
        r_value = float(value or 0.0)
        equity += r_value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        if r_value < 0:
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0
        mfe = _mfe_r(row)
        if mfe is not None and mfe > 0:
            captures.append(max(-1.0, min(1.5, r_value / mfe)))
    capture = sum(captures) / len(captures) if captures else None
    return {
        "strategy": wanted,
        "trade_count": len(rows),
        "r_samples": len(values),
        "expectancy_r": expectancy_r,
        "profit_factor": profit_factor,
        "max_drawdown_r": drawdown,
        "max_consecutive_losses": max_loss_streak,
        "current_consecutive_losses": loss_streak,
        "mfe_capture_ratio": capture,
        "net_pnl": sum(pnl_values),
    }


@dataclass(frozen=True)
class StrategyAllocation:
    strategy: str
    multiplier: float = 1.0
    reason: str = "neutral"
    metrics: dict[str, Any] = field(default_factory=dict)


def evaluate_strategy_allocation(
    metrics: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> StrategyAllocation:
    cfg = {**default_strategy_allocator_config(), **dict(config or {})}
    values = dict(metrics or {})
    strategy = str(values.get("strategy") or "unknown")
    samples = int(values.get("trade_count") or 0)
    minimum = max(1, int(cfg["minimum_samples"]))
    if not cfg.get("enabled", True):
        return StrategyAllocation(strategy, 1.0, "allocator_disabled", values)
    if samples < minimum:
        return StrategyAllocation(strategy, 1.0, f"insufficient_samples:{samples}/{minimum}", values)

    multiplier = 1.0
    reasons: list[str] = []
    expectancy = float(_finite(values.get("expectancy_r"), 0.0) or 0.0)
    profit_factor = float(_finite(values.get("profit_factor"), 0.0) or 0.0)
    loss_streak = int(values.get("current_consecutive_losses") or 0)
    drawdown = float(_finite(values.get("max_drawdown_r"), 0.0) or 0.0)
    capture = _finite(values.get("mfe_capture_ratio"))
    if expectancy < 0:
        multiplier *= float(cfg["negative_expectancy_multiplier"])
        reasons.append("negative_expectancy")
    elif expectancy >= float(cfg["good_expectancy_r"]):
        reasons.append("positive_expectancy")
    if profit_factor < 1.0:
        multiplier *= float(cfg["weak_profit_factor_multiplier"])
        reasons.append("profit_factor_below_1")
    elif profit_factor >= float(cfg["good_profit_factor"]):
        reasons.append("profit_factor_good")
    if loss_streak >= int(cfg["loss_streak_threshold"]):
        multiplier *= float(cfg["loss_streak_multiplier"])
        reasons.append("loss_streak")
    if drawdown >= float(cfg["drawdown_r_threshold"]):
        multiplier *= float(cfg["drawdown_multiplier"])
        reasons.append("drawdown")
    if capture is not None and capture < float(cfg["mfe_capture_min"]):
        multiplier *= float(cfg["poor_capture_multiplier"])
        reasons.append("poor_mfe_capture")

    full_samples = max(minimum, int(cfg["full_confidence_samples"]))
    confidence = min(1.0, max(0.0, (samples - minimum + 1) / max(full_samples - minimum + 1, 1)))
    multiplier = 1.0 - (1.0 - multiplier) * confidence
    multiplier = max(float(cfg["risk_floor"]), min(float(cfg["risk_cap"]), multiplier))
    values["confidence"] = confidence
    return StrategyAllocation(
        strategy=strategy,
        multiplier=multiplier,
        reason=",".join(reasons) if reasons else "neutral_performance",
        metrics=values,
    )


def scale_plan_risk(plan: Mapping[str, Any], multiplier: float) -> dict[str, Any]:
    scaled = dict(plan or {})
    multiplier = max(0.0, min(1.0, float(multiplier or 0.0)))
    for key in (
        "qty",
        "risk_usdt",
        "max_risk_per_trade_usdt",
        "planned_notional",
        "planned_margin",
        "expected_profit_usdt",
        "position_notional",
    ):
        value = _finite(scaled.get(key))
        if value is not None:
            scaled[key] = value * multiplier
    percent = _finite(scaled.get("risk_per_trade_percent"))
    if percent is not None:
        scaled["risk_per_trade_percent"] = percent * multiplier
    return scaled

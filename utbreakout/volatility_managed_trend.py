"""Low-turnover, volatility-managed time-series trend strategy.

The strategy deliberately uses a small set of broad, volatility-normalized
rules.  It combines several return horizons, requires a persistent 1h trend,
avoids choppy/extended entries, and leaves final liquidity/order-book checks to
the shared live execution path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite, log, sqrt
from statistics import median, pstdev
from typing import Any, Mapping, Sequence


VOLATILITY_MANAGED_TREND_STRATEGY = "volatility_managed_trend_v1"


def default_volatility_managed_trend_config() -> dict[str, Any]:
    """Return broad institutional-style trend and risk defaults."""

    return {
        "enabled": True,
        "live_enabled": True,
        "timeframe": "1h",
        "lookback_bars": (8, 24, 72),
        "minimum_horizon_agreement": 2,
        "normalized_return_min": 0.30,
        "fast_ema_period": 12,
        "slow_ema_period": 36,
        "ema_slope_bars": 3,
        "atr_period": 14,
        "efficiency_period": 24,
        "efficiency_min": 0.24,
        "short_volatility_period": 12,
        "long_volatility_period": 48,
        "volatility_shock_ratio_max": 1.75,
        "target_hourly_volatility": 0.012,
        "extension_max_atr": 1.80,
        "latest_range_max_atr": 2.20,
        "volume_lookback_bars": 48,
        "volume_ratio_min": 0.50,
        "score_min": 66.0,
        "risk_multiplier_floor": 0.25,
        "risk_multiplier_cap": 0.60,
        "structure_lookback_bars": 12,
        "structure_buffer_atr": 0.10,
        "stop_atr_multiplier": 1.60,
        "take_profit_r_multiple": 3.00,
        "time_stop_bars": 24,
        "entry_chase_max_atr": 0.50,
    }


@dataclass(frozen=True)
class VolatilityManagedTrendDecision:
    side: str | None = None
    allowed: bool = False
    score: float = 0.0
    risk_multiplier: float = 0.0
    reason: str = "not_evaluated"
    metrics: dict[str, Any] = field(default_factory=dict)


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clean_rows(rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for raw in rows or []:
        values = {key: _finite((raw or {}).get(key)) for key in ("open", "high", "low", "close")}
        if any(values[key] is None or values[key] <= 0 for key in values):
            continue
        volume = max(0.0, float(_finite((raw or {}).get("volume"), 0.0) or 0.0))
        cleaned.append({
            "timestamp": (raw or {}).get("timestamp"),
            "open": float(values["open"]),
            "high": float(values["high"]),
            "low": float(values["low"]),
            "close": float(values["close"]),
            "volume": volume,
        })
    return cleaned


def _ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (max(2, int(period)) + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def _atr(rows: Sequence[Mapping[str, Any]], period: int) -> float | None:
    if len(rows) < period + 1:
        return None
    ranges: list[float] = []
    for index in range(1, len(rows)):
        high = float(rows[index]["high"])
        low = float(rows[index]["low"])
        previous_close = float(rows[index - 1]["close"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    window = ranges[-max(2, int(period)):]
    value = sum(window) / len(window) if window else 0.0
    return value if value > 0 and isfinite(value) else None


def _log_returns(closes: Sequence[float]) -> list[float]:
    return [log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]


def evaluate_volatility_managed_trend(
    rows: Sequence[Mapping[str, Any]] | None,
    l2_gate: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> VolatilityManagedTrendDecision:
    """Evaluate a completed-candle multi-horizon trend without placing an order."""

    cfg = {**default_volatility_managed_trend_config(), **dict(config or {})}
    candles = _clean_rows(rows)
    horizons = tuple(sorted({max(2, int(value)) for value in cfg["lookback_bars"]}))
    long_vol_period = max(12, int(cfg["long_volatility_period"]))
    minimum_rows = max(
        max(horizons) + 2,
        long_vol_period + 2,
        int(cfg["slow_ema_period"]) + int(cfg["ema_slope_bars"]) + 2,
    )
    if len(candles) < minimum_rows:
        return VolatilityManagedTrendDecision(reason="insufficient_completed_candles")

    closes = [float(row["close"]) for row in candles]
    returns = _log_returns(closes)
    atr_value = _atr(candles, max(2, int(cfg["atr_period"])))
    if atr_value is None:
        return VolatilityManagedTrendDecision(reason="atr_unavailable")

    short_period = max(4, int(cfg["short_volatility_period"]))
    short_vol = pstdev(returns[-short_period:]) if len(returns) >= short_period else 0.0
    long_vol = pstdev(returns[-long_vol_period:]) if len(returns) >= long_vol_period else 0.0
    if long_vol <= 0 or not isfinite(long_vol):
        return VolatilityManagedTrendDecision(reason="realized_volatility_unavailable")
    volatility_ratio = short_vol / long_vol if long_vol > 0 else float("inf")

    normalized_returns: dict[int, float] = {}
    votes: dict[int, str | None] = {}
    signal_floor = max(0.0, float(cfg["normalized_return_min"]))
    for horizon in horizons:
        raw_return = closes[-1] / closes[-1 - horizon] - 1.0
        normalized = raw_return / max(long_vol * sqrt(float(horizon)), 1e-9)
        normalized_returns[horizon] = normalized
        votes[horizon] = "long" if normalized >= signal_floor else "short" if normalized <= -signal_floor else None

    long_votes = sum(1 for value in votes.values() if value == "long")
    short_votes = sum(1 for value in votes.values() if value == "short")
    required_votes = max(2, min(len(horizons), int(cfg["minimum_horizon_agreement"])))
    side = "long" if long_votes >= required_votes else "short" if short_votes >= required_votes else None
    metrics: dict[str, Any] = {
        "atr": atr_value,
        "reference_price": closes[-1],
        "signal_candle_ts": candles[-1].get("timestamp"),
        "normalized_returns": normalized_returns,
        "horizon_votes": votes,
        "long_votes": long_votes,
        "short_votes": short_votes,
        "required_votes": required_votes,
        "short_volatility": short_vol,
        "long_volatility": long_vol,
        "volatility_ratio": volatility_ratio,
        "l2_gate": dict(l2_gate or {}),
    }
    if side is None:
        return VolatilityManagedTrendDecision(reason="multi_horizon_trend_not_aligned", metrics=metrics)
    slow_vote = votes[max(horizons)]
    if slow_vote not in {None, side}:
        return VolatilityManagedTrendDecision(side=side, reason="slow_horizon_conflict", metrics=metrics)
    if volatility_ratio > float(cfg["volatility_shock_ratio_max"]):
        return VolatilityManagedTrendDecision(side=side, reason="volatility_shock", metrics=metrics)

    fast_ema = _ema(closes, int(cfg["fast_ema_period"]))
    slow_ema = _ema(closes, int(cfg["slow_ema_period"]))
    slope_bars = max(1, int(cfg["ema_slope_bars"]))
    fast_slope = (fast_ema[-1] - fast_ema[-1 - slope_bars]) / atr_value
    trend_aligned = (
        closes[-1] > fast_ema[-1] > slow_ema[-1] and fast_slope > 0
        if side == "long"
        else closes[-1] < fast_ema[-1] < slow_ema[-1] and fast_slope < 0
    )
    metrics.update({
        "fast_ema": fast_ema[-1],
        "slow_ema": slow_ema[-1],
        "fast_ema_slope_atr": fast_slope,
        "trend_aligned": trend_aligned,
    })
    if not trend_aligned:
        return VolatilityManagedTrendDecision(side=side, reason="ema_trend_not_aligned", metrics=metrics)

    efficiency_period = max(4, int(cfg["efficiency_period"]))
    efficiency_window = closes[-efficiency_period - 1:]
    path = sum(abs(efficiency_window[index] - efficiency_window[index - 1]) for index in range(1, len(efficiency_window)))
    efficiency = abs(efficiency_window[-1] - efficiency_window[0]) / path if path > 0 else 0.0
    extension_atr = abs(closes[-1] - fast_ema[-1]) / atr_value
    latest_range_atr = (float(candles[-1]["high"]) - float(candles[-1]["low"])) / atr_value
    volume_period = max(8, int(cfg["volume_lookback_bars"]))
    volume_baseline = median([float(row["volume"]) for row in candles[-volume_period - 1:-1]])
    volume_ratio = float(candles[-1]["volume"]) / volume_baseline if volume_baseline > 0 else 1.0
    structure_window = candles[-max(3, int(cfg["structure_lookback_bars"])) - 1:-1]
    structure_stop = (
        min(float(row["low"]) for row in structure_window)
        if side == "long"
        else max(float(row["high"]) for row in structure_window)
    )
    metrics.update({
        "efficiency_ratio": efficiency,
        "extension_atr": extension_atr,
        "latest_range_atr": latest_range_atr,
        "volume_ratio": volume_ratio,
        "structure_stop": structure_stop,
    })
    if efficiency < float(cfg["efficiency_min"]):
        return VolatilityManagedTrendDecision(side=side, reason="trend_efficiency_too_low", metrics=metrics)
    if extension_atr > float(cfg["extension_max_atr"]):
        return VolatilityManagedTrendDecision(side=side, reason="entry_too_extended", metrics=metrics)
    if latest_range_atr > float(cfg["latest_range_max_atr"]):
        return VolatilityManagedTrendDecision(side=side, reason="latest_bar_volatility_shock", metrics=metrics)
    if volume_ratio < float(cfg["volume_ratio_min"]):
        return VolatilityManagedTrendDecision(side=side, reason="volume_confirmation_missing", metrics=metrics)
    if l2_gate is not None and not bool((l2_gate or {}).get("allowed", False)):
        return VolatilityManagedTrendDecision(side=side, reason="l2_stressed", metrics=metrics)

    aligned_strength = sorted(abs(value) for value in normalized_returns.values() if value * (1 if side == "long" else -1) > 0)
    median_strength = median(aligned_strength) if aligned_strength else 0.0
    score = 40.0 + 7.5 * max(long_votes, short_votes)
    score += min(15.0, efficiency * 30.0)
    score += min(10.0, median_strength * 4.0)
    score += min(5.0, max(0.0, volume_ratio - 0.5) * 5.0)
    score = min(100.0, score)
    metrics["score"] = score
    if score < float(cfg["score_min"]):
        return VolatilityManagedTrendDecision(side=side, score=score, reason="score_below_threshold", metrics=metrics)

    target_vol = max(1e-6, float(cfg["target_hourly_volatility"]))
    volatility_scale = min(1.0, target_vol / max(short_vol, long_vol, 1e-9))
    l2_multiplier = float((l2_gate or {}).get("risk_multiplier", 1.0) or 0.0)
    risk = min(
        float(cfg["risk_multiplier_cap"]),
        max(float(cfg["risk_multiplier_floor"]), score / 100.0 * volatility_scale),
        max(0.0, l2_multiplier),
    )
    metrics["volatility_scale"] = volatility_scale
    return VolatilityManagedTrendDecision(
        side=side,
        allowed=True,
        score=score,
        risk_multiplier=max(0.0, risk),
        reason=(
            f"VMT {side} score={score:.1f} horizons={max(long_votes, short_votes)}/{len(horizons)} "
            f"efficiency={efficiency:.2f} vol-scale={volatility_scale:.2f}"
        ),
        metrics=metrics,
    )


__all__ = (
    "VOLATILITY_MANAGED_TREND_STRATEGY",
    "VolatilityManagedTrendDecision",
    "default_volatility_managed_trend_config",
    "evaluate_volatility_managed_trend",
)

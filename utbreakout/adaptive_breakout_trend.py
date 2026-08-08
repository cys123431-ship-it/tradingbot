"""Adaptive multi-horizon breakout trend signal.

The model deliberately keeps forecasting simple and interpretable.  Direction
comes from volatility-normalised time-series momentum across several horizons;
entries require either a fresh channel breakout or a shorter re-acceleration in
the same established trend.  Moving averages are alignment aids, not the
primary signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite, log, sqrt
from statistics import median
from typing import Any, Mapping, Sequence


ADAPTIVE_BREAKOUT_TREND_STRATEGY = "adaptive_breakout_trend_v1"


def default_adaptive_breakout_trend_config() -> dict[str, Any]:
    """Broad defaults intended to remain stable across liquid crypto futures."""

    return {
        "enabled": True,
        "live_enabled": False,
        "universe_mode": "auto",
        "single_symbol": "",
        "timeframe": "1h",
        "fetch_limit": 360,
        "momentum_horizons": (24, 72, 168),
        "momentum_weights": (0.25, 0.35, 0.40),
        "minimum_horizon_agreement": 2,
        "minimum_momentum_strength": 0.18,
        "fast_ema_period": 12,
        "medium_ema_period": 48,
        "slow_ema_period": 144,
        "ema_slope_bars": 6,
        "channel_lookback_bars": 48,
        "reacceleration_lookback_bars": 12,
        "atr_period": 20,
        "volatility_short_bars": 24,
        "volatility_long_bars": 96,
        "target_hourly_volatility": 0.012,
        "volatility_targeting_power": 0.50,
        "volatility_risk_floor": 0.75,
        "volatility_risk_cap": 1.10,
        "volatility_shock_ratio": 3.00,
        "efficiency_lookback_bars": 48,
        "minimum_trend_efficiency": 0.16,
        "latest_range_max_atr": 2.80,
        "entry_chase_max_atr": 0.80,
        "structure_lookback_bars": 20,
        "structure_buffer_atr": 0.15,
        "stop_atr_multiplier": 2.00,
        "take_profit_r_multiple": 4.00,
        "score_min": 62.0,
        "base_risk_multiplier": 0.80,
        "strong_risk_multiplier": 0.95,
        "elite_risk_multiplier": 1.00,
        "strong_score": 76.0,
        "elite_score": 88.0,
        "time_stop_hours": 168,
    }


def normalize_adaptive_breakout_trend_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable trend configuration, including its scan universe."""

    normalized = default_adaptive_breakout_trend_config()
    if isinstance(config, Mapping):
        normalized.update(dict(config))

    universe_mode = str(
        normalized.get("universe_mode", "auto") or "auto"
    ).strip().lower()
    if universe_mode not in {"auto", "single"}:
        universe_mode = "auto"
    single_symbol = str(normalized.get("single_symbol", "") or "").strip().upper()
    normalized["universe_mode"] = universe_mode
    normalized["single_symbol"] = single_symbol
    return normalized


@dataclass(frozen=True)
class AdaptiveBreakoutTrendDecision:
    allowed: bool = False
    side: str | None = None
    score: float = 0.0
    risk_multiplier: float = 0.0
    reason: str = "waiting"
    metrics: dict[str, Any] = field(default_factory=dict)


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def _clean_rows(rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for row in rows or ():
        close = _finite(row.get("close"))
        high = _finite(row.get("high"))
        low = _finite(row.get("low"))
        open_price = _finite(row.get("open"), close)
        volume = _finite(row.get("volume"), 0.0)
        if close is None or high is None or low is None or close <= 0:
            continue
        if high < low or high <= 0 or low <= 0:
            continue
        cleaned.append({
            "timestamp": row.get("timestamp"),
            "open": open_price if open_price is not None else close,
            "high": high,
            "low": low,
            "close": close,
            "volume": max(0.0, volume or 0.0),
        })
    return cleaned


def _ema(values: Sequence[float], period: int) -> list[float]:
    period = max(2, int(period))
    alpha = 2.0 / (period + 1.0)
    result: list[float] = []
    current = float(values[0])
    for value in values:
        current = alpha * float(value) + (1.0 - alpha) * current
        result.append(current)
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
    return sum(window) / len(window) if window else None


def _log_returns(closes: Sequence[float]) -> list[float]:
    return [log(float(closes[i]) / float(closes[i - 1])) for i in range(1, len(closes))]


def _rms(values: Sequence[float]) -> float:
    return sqrt(sum(float(value) ** 2 for value in values) / len(values)) if values else 0.0


def _bounded(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _normalized_momentum_horizons(
    horizons_value: Any,
    weights_value: Any,
) -> tuple[tuple[int, ...], tuple[float, ...]]:
    """Normalize horizon/weight pairs without changing their configured mapping."""

    default_cfg = default_adaptive_breakout_trend_config()
    try:
        raw_horizons = tuple(horizons_value)
    except TypeError:
        raw_horizons = ()
    try:
        parsed_horizons = tuple(max(4, int(value)) for value in raw_horizons)
    except (TypeError, ValueError):
        parsed_horizons = ()
    if not parsed_horizons:
        parsed_horizons = tuple(
            max(4, int(value)) for value in default_cfg['momentum_horizons']
        )

    try:
        raw_weights = tuple(float(value) for value in weights_value)
    except (TypeError, ValueError):
        raw_weights = ()
    if (
        len(raw_weights) != len(parsed_horizons)
        or sum(max(0.0, value) for value in raw_weights) <= 0
    ):
        raw_weights = tuple(1.0 for _ in parsed_horizons)

    combined: dict[int, float] = {}
    for horizon, weight in zip(parsed_horizons, raw_weights):
        combined[horizon] = combined.get(horizon, 0.0) + max(0.0, weight)
    ordered = tuple(sorted(combined.items()))
    weight_sum = sum(weight for _, weight in ordered)
    if weight_sum <= 0:
        ordered = tuple((horizon, 1.0) for horizon, _ in ordered)
        weight_sum = float(len(ordered))
    return (
        tuple(horizon for horizon, _ in ordered),
        tuple(weight / weight_sum for _, weight in ordered),
    )


def evaluate_adaptive_breakout_trend(
    rows: Sequence[Mapping[str, Any]] | None,
    l2_gate: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> AdaptiveBreakoutTrendDecision:
    """Evaluate completed candles without placing or modifying an order."""

    cfg = {**default_adaptive_breakout_trend_config(), **dict(config or {})}
    candles = _clean_rows(rows)
    horizons, weights = _normalized_momentum_horizons(
        cfg.get("momentum_horizons"),
        cfg.get("momentum_weights"),
    )
    required = max(
        max(horizons) + 2,
        int(cfg["slow_ema_period"]) + int(cfg["ema_slope_bars"]) + 2,
        int(cfg["volatility_long_bars"]) + 2,
        int(cfg["channel_lookback_bars"]) + 2,
    )
    if len(candles) < required:
        return AdaptiveBreakoutTrendDecision(reason="insufficient_completed_candles")

    closes = [float(row["close"]) for row in candles]
    returns = _log_returns(closes)
    atr_value = _atr(candles, max(2, int(cfg["atr_period"])))
    if atr_value is None or atr_value <= 0:
        return AdaptiveBreakoutTrendDecision(reason="atr_unavailable")

    short_window = returns[-max(4, int(cfg["volatility_short_bars"])):]
    long_window = returns[-max(8, int(cfg["volatility_long_bars"])):]
    short_vol = _rms(short_window)
    long_vol = _rms(long_window)
    if short_vol <= 0 or long_vol <= 0:
        return AdaptiveBreakoutTrendDecision(reason="realized_volatility_unavailable")

    horizon_scores: dict[int, float] = {}
    horizon_votes: dict[int, str | None] = {}
    weighted_momentum = 0.0
    for horizon, weight in zip(horizons, weights):
        raw_return = log(closes[-1] / closes[-1 - horizon])
        normalized = raw_return / max(long_vol * sqrt(float(horizon)), 1e-9)
        clipped = _bounded(normalized / 2.0, -1.0, 1.0)
        horizon_scores[horizon] = normalized
        weighted_momentum += clipped * weight
        horizon_votes[horizon] = "long" if normalized > 0.10 else "short" if normalized < -0.10 else None

    long_votes = sum(value == "long" for value in horizon_votes.values())
    short_votes = sum(value == "short" for value in horizon_votes.values())
    minimum_votes = max(2, min(len(horizons), int(cfg["minimum_horizon_agreement"])))
    side = "long" if weighted_momentum > 0 else "short" if weighted_momentum < 0 else None
    dominant_votes = long_votes if side == "long" else short_votes if side == "short" else 0

    fast_ema = _ema(closes, int(cfg["fast_ema_period"]))
    medium_ema = _ema(closes, int(cfg["medium_ema_period"]))
    slow_ema = _ema(closes, int(cfg["slow_ema_period"]))
    slope_bars = max(1, int(cfg["ema_slope_bars"]))
    medium_slope_atr = (medium_ema[-1] - medium_ema[-1 - slope_bars]) / atr_value
    ema_aligned = bool(
        side == "long"
        and closes[-1] > medium_ema[-1] > slow_ema[-1]
        and medium_slope_atr > 0
    ) or bool(
        side == "short"
        and closes[-1] < medium_ema[-1] < slow_ema[-1]
        and medium_slope_atr < 0
    )

    channel_period = max(8, int(cfg["channel_lookback_bars"]))
    reacceleration_period = max(4, int(cfg["reacceleration_lookback_bars"]))
    previous_channel = candles[-channel_period - 1:-1]
    previous_reacceleration = candles[-reacceleration_period - 1:-1]
    channel_high = max(float(row["high"]) for row in previous_channel)
    channel_low = min(float(row["low"]) for row in previous_channel)
    reacceleration_high = max(float(row["high"]) for row in previous_reacceleration)
    reacceleration_low = min(float(row["low"]) for row in previous_reacceleration)
    fresh_breakout = bool(
        side == "long" and closes[-1] > channel_high
    ) or bool(
        side == "short" and closes[-1] < channel_low
    )
    reacceleration = bool(
        side == "long"
        and closes[-1] > reacceleration_high
        and fast_ema[-1] > medium_ema[-1]
    ) or bool(
        side == "short"
        and closes[-1] < reacceleration_low
        and fast_ema[-1] < medium_ema[-1]
    )

    efficiency_period = max(8, int(cfg["efficiency_lookback_bars"]))
    efficiency_closes = closes[-efficiency_period - 1:]
    path = sum(abs(efficiency_closes[i] - efficiency_closes[i - 1]) for i in range(1, len(efficiency_closes)))
    efficiency = abs(efficiency_closes[-1] - efficiency_closes[0]) / max(path, 1e-9)
    latest_range_atr = (
        float(candles[-1]["high"]) - float(candles[-1]["low"])
    ) / atr_value
    volatility_ratio = short_vol / max(long_vol, 1e-9)
    fast_vote = horizon_votes[min(horizons)]
    slow_vote = horizon_votes[max(horizons)]
    turning_conflict = bool(
        side in {"long", "short"}
        and slow_vote == side
        and fast_vote not in {None, side}
        and not fresh_breakout
    )

    volumes = [float(row.get("volume") or 0.0) for row in candles]
    baseline_volume = median(volumes[-49:-1]) if len(volumes) >= 49 else median(volumes[:-1])
    volume_ratio = volumes[-1] / max(baseline_volume, 1e-9) if baseline_volume > 0 else 1.0
    structure_window = candles[-max(4, int(cfg["structure_lookback_bars"])) - 1:-1]
    structure_stop = (
        min(float(row["low"]) for row in structure_window)
        if side == "long"
        else max(float(row["high"]) for row in structure_window)
        if side == "short"
        else None
    )

    metrics = {
        "reference_price": closes[-1],
        "signal_candle_ts": candles[-1].get("timestamp"),
        "atr": atr_value,
        "short_volatility": short_vol,
        "long_volatility": long_vol,
        "volatility_ratio": volatility_ratio,
        "weighted_momentum": weighted_momentum,
        "horizon_scores": horizon_scores,
        "horizon_votes": horizon_votes,
        "long_votes": long_votes,
        "short_votes": short_votes,
        "fast_ema": fast_ema[-1],
        "medium_ema": medium_ema[-1],
        "slow_ema": slow_ema[-1],
        "medium_ema_slope_atr": medium_slope_atr,
        "ema_aligned": ema_aligned,
        "channel_high": channel_high,
        "channel_low": channel_low,
        "fresh_breakout": fresh_breakout,
        "reacceleration": reacceleration,
        "turning_conflict": turning_conflict,
        "trend_efficiency": efficiency,
        "latest_range_atr": latest_range_atr,
        "volume_ratio": volume_ratio,
        "structure_stop": structure_stop,
    }

    if side is None or dominant_votes < minimum_votes:
        return AdaptiveBreakoutTrendDecision(side=side, reason="multi_horizon_direction_not_aligned", metrics=metrics)
    if abs(weighted_momentum) < float(cfg["minimum_momentum_strength"]):
        return AdaptiveBreakoutTrendDecision(side=side, reason="momentum_strength_too_low", metrics=metrics)
    if slow_vote not in {None, side}:
        return AdaptiveBreakoutTrendDecision(side=side, reason="slow_horizon_conflict", metrics=metrics)
    if not ema_aligned:
        return AdaptiveBreakoutTrendDecision(side=side, reason="trend_structure_not_aligned", metrics=metrics)
    if turning_conflict:
        return AdaptiveBreakoutTrendDecision(side=side, reason="fast_slow_turning_conflict", metrics=metrics)
    if not fresh_breakout and not reacceleration:
        return AdaptiveBreakoutTrendDecision(side=side, reason="waiting_for_channel_breakout", metrics=metrics)
    if efficiency < float(cfg["minimum_trend_efficiency"]):
        return AdaptiveBreakoutTrendDecision(side=side, reason="trend_efficiency_too_low", metrics=metrics)
    if volatility_ratio > float(cfg["volatility_shock_ratio"]):
        return AdaptiveBreakoutTrendDecision(side=side, reason="volatility_shock", metrics=metrics)
    if latest_range_atr > float(cfg["latest_range_max_atr"]):
        return AdaptiveBreakoutTrendDecision(side=side, reason="latest_bar_extreme_range", metrics=metrics)
    if l2_gate is not None and not bool(l2_gate.get("allowed", False)):
        return AdaptiveBreakoutTrendDecision(side=side, reason="l2_stressed", metrics=metrics)

    score = 44.0
    score += min(18.0, abs(weighted_momentum) * 24.0)
    score += min(12.0, dominant_votes * 4.0)
    score += 8.0 if fresh_breakout else 5.0
    score += min(10.0, efficiency * 22.0)
    score += min(4.0, max(0.0, volume_ratio - 0.70) * 3.0)
    if slow_vote == side:
        score += 4.0
    score = min(100.0, score)
    metrics["score"] = score
    if score < float(cfg["score_min"]):
        return AdaptiveBreakoutTrendDecision(side=side, score=score, reason="score_below_threshold", metrics=metrics)

    target_vol = max(1e-6, float(cfg["target_hourly_volatility"]))
    targeting_power = _bounded(float(cfg["volatility_targeting_power"]), 0.0, 1.0)
    raw_volatility_scale = (target_vol / max(short_vol, long_vol, 1e-9)) ** targeting_power
    volatility_scale = _bounded(
        raw_volatility_scale,
        float(cfg["volatility_risk_floor"]),
        float(cfg["volatility_risk_cap"]),
    )
    if score >= float(cfg["elite_score"]):
        quality_risk = float(cfg["elite_risk_multiplier"])
        risk_tier = "elite"
    elif score >= float(cfg["strong_score"]):
        quality_risk = float(cfg["strong_risk_multiplier"])
        risk_tier = "strong"
    else:
        quality_risk = float(cfg["base_risk_multiplier"])
        risk_tier = "base"
    l2_multiplier = float((l2_gate or {}).get("risk_multiplier", 1.0) or 0.0)
    risk_multiplier = _bounded(quality_risk * volatility_scale, 0.0, 1.0)
    if l2_gate is not None:
        risk_multiplier = min(risk_multiplier, max(0.0, l2_multiplier))
    metrics.update({
        "raw_volatility_scale": raw_volatility_scale,
        "volatility_scale": volatility_scale,
        "quality_risk_multiplier": quality_risk,
        "risk_tier": risk_tier,
        "risk_multiplier": risk_multiplier,
    })
    mode = "fresh breakout" if fresh_breakout else "trend re-acceleration"
    return AdaptiveBreakoutTrendDecision(
        allowed=True,
        side=side,
        score=score,
        risk_multiplier=risk_multiplier,
        reason=(
            f"Adaptive Breakout Trend {side} {mode}: score={score:.1f} "
            f"momentum={weighted_momentum:+.2f} votes={dominant_votes}/{len(horizons)} "
            f"risk={risk_multiplier:.2f}"
        ),
        metrics=metrics,
    )


__all__ = (
    "ADAPTIVE_BREAKOUT_TREND_STRATEGY",
    "AdaptiveBreakoutTrendDecision",
    "default_adaptive_breakout_trend_config",
    "evaluate_adaptive_breakout_trend",
    "normalize_adaptive_breakout_trend_config",
)

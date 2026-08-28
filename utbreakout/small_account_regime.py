"""Regime-routed challenger for the aggressive sub-$1,000 trend profile.

The existing adaptive trend and change-point engines remain the primary entry
paths.  This module adds one deliberately narrow alternative: a confirmed
liquidity sweep/reclaim while the broad 1h trend is weak or already agrees
with the reversal direction.  It never overrides a valid trend/event entry.

All helpers are pure.  They evaluate completed candles and return plan
metadata; they never place, cancel, or modify an exchange order.
"""

from __future__ import annotations

from collections import Counter
from math import isfinite
from statistics import median
from typing import Any, Mapping, Sequence


SMALL_ACCOUNT_REGIME_PROFILE_VERSION = "small_account_regime_ensemble_v4_risk_managed_momentum"


def default_small_account_regime_config() -> dict[str, Any]:
    return {
        "profile_version": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
        "enabled": True,
        "crypto_only": True,
        "sweep_lookback_bars": 12,
        "volume_lookback_bars": 24,
        "minimum_volume_ratio": 1.20,
        "minimum_wick_ratio": 0.30,
        "minimum_reclaim_close_location": 0.60,
        "minimum_sweep_depth_atr": 0.04,
        "maximum_sweep_depth_atr": 1.25,
        "strong_opposite_momentum_block": 0.42,
        "strong_opposite_vote_block": 2,
        "maximum_countertrend_clarity": 0.48,
        "minimum_vwap_extension_atr": 0.30,
        "minimum_range_extension_atr": 0.04,
        "minimum_score": 68.0,
        "minimum_nonprice_confirmations": 1,
        "orderflow_imbalance_confirm_pct": 2.0,
        "taker_ratio_confirm_delta": 0.02,
        "funding_percentile_confirm": 80.0,
        "long_short_ratio_long_confirm": 0.82,
        "long_short_ratio_short_confirm": 1.22,
        "open_interest_z_confirm": 0.35,
        "initial_margin_fraction": 0.65,
        "risk_tier": "base",
        "minimum_stop_atr": 0.70,
        "maximum_stop_atr": 1.60,
        "stop_buffer_atr": 0.12,
        "tp1_ratio": 0.45,
        "tp2_ratio": 0.55,
        "tp1_r_floor": 0.55,
        "tp1_r_cap": 0.90,
        "tp2_r_floor": 1.00,
        "tp2_r_cap": 1.80,
        "time_stop_bars": 12,
        "multi_timeframe_weights": {
            "15m": 0.15,
            "1h": 0.30,
            "4h": 0.35,
            "1d": 0.20,
        },
        # Blend complementary trend speeds inside every timeframe.  This is a
        # weighted ensemble, not an all-speeds-must-agree entry gate.
        "multi_speed_weights": {
            "fast": 0.40,
            "medium": 0.35,
            "slow": 0.25,
        },
        "state_persistence_lag_bars": 4,
        "orderflow_freshness_seconds": 90.0,
        "multi_timeframe_minimum_direction_score": 0.18,
        "multi_timeframe_strong_direction_score": 0.42,
        "multi_timeframe_max_disagreements": 2,
        "trend_maturity_extension_atr": 2.40,
        "trend_maturity_run_bars": 18,
        "router_minimum_net_ev_r": 0.02,
        "router_conflict_net_ev_gap_r": 0.12,
        "cross_sectional_top_only": True,
        "cross_sectional_minimum_score_gap": 2.50,
        "estimated_round_trip_fee_r": 0.035,
        "estimated_slippage_r": 0.025,
        "estimated_missed_fill_r": 0.015,
        # This overlay never reduces or blocks an entry. It only increases the
        # first-stage margin for unusually strong crypto long momentum where
        # relative rank, persistent state, complementary speeds and fresh
        # signed flow all corroborate.
        "evidence_allocation_enabled": True,
        "evidence_strong_score": 70.0,
        "evidence_elite_score": 82.0,
        "evidence_strong_percentile": 75.0,
        "evidence_elite_percentile": 90.0,
        "evidence_strong_speed_agreement": 0.65,
        "evidence_elite_speed_agreement": 0.80,
        "evidence_strong_persistence": 0.60,
        "evidence_elite_persistence": 0.75,
        "evidence_strong_margin_multiplier": 1.15,
        "evidence_elite_margin_multiplier": 1.25,
        "evidence_max_initial_margin_fraction": 0.90,
        "evidence_minimum_universe_size": 5,
        "promotion_required": True,
        "auto_promote_when_qualified": True,
        "promotion_lookback_days": 365,
        "promotion_min_trades": 100,
        "promotion_min_regime_trades": 15,
        "promotion_min_expectancy_r": 0.08,
        "promotion_min_profit_factor": 1.15,
        "promotion_min_calmar": 1.00,
        "promotion_max_pbo": 0.45,
        "promotion_max_symbol_concentration": 0.35,
        "promotion_multiple_testing_trials": 24,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _bounded(value: Any, lower: float, upper: float, default: float) -> float:
    parsed = _finite(value, default)
    return max(lower, min(upper, float(parsed if parsed is not None else default)))


def normalize_small_account_regime_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = default_small_account_regime_config()
    normalized = dict(defaults)
    if isinstance(config, Mapping):
        normalized.update(config)
    for key in (
        "enabled",
        "crypto_only",
        "promotion_required",
        "auto_promote_when_qualified",
        "cross_sectional_top_only",
        "evidence_allocation_enabled",
    ):
        raw = normalized.get(key, defaults[key])
        normalized[key] = (
            raw
            if isinstance(raw, bool)
            else str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}
        )
    for key, lower, upper in (
        ("sweep_lookback_bars", 6, 48),
        ("volume_lookback_bars", 12, 96),
        ("strong_opposite_vote_block", 1, 4),
        ("minimum_nonprice_confirmations", 1, 4),
        ("time_stop_bars", 4, 48),
        ("multi_timeframe_max_disagreements", 0, 4),
        ("state_persistence_lag_bars", 2, 12),
        ("trend_maturity_run_bars", 4, 96),
        ("promotion_lookback_days", 30, 730),
        ("promotion_min_trades", 100, 200),
        ("promotion_min_regime_trades", 5, 60),
        ("promotion_multiple_testing_trials", 1, 500),
        ("evidence_minimum_universe_size", 2, 200),
    ):
        normalized[key] = int(
            _bounded(normalized.get(key), float(lower), float(upper), float(defaults[key]))
        )
    for key, lower, upper in (
        ("minimum_volume_ratio", 0.80, 4.00),
        ("minimum_wick_ratio", 0.10, 0.80),
        ("minimum_reclaim_close_location", 0.50, 0.95),
        ("minimum_sweep_depth_atr", 0.0, 0.50),
        ("maximum_sweep_depth_atr", 0.25, 3.00),
        ("strong_opposite_momentum_block", 0.10, 1.00),
        ("maximum_countertrend_clarity", 0.10, 0.90),
        ("minimum_vwap_extension_atr", 0.0, 3.0),
        ("minimum_range_extension_atr", 0.0, 0.75),
        ("minimum_score", 50.0, 90.0),
        ("orderflow_imbalance_confirm_pct", 0.25, 20.0),
        ("taker_ratio_confirm_delta", 0.005, 0.25),
        ("funding_percentile_confirm", 50.0, 99.0),
        ("long_short_ratio_long_confirm", 0.20, 0.98),
        ("long_short_ratio_short_confirm", 1.02, 5.00),
        ("open_interest_z_confirm", -1.0, 3.0),
        ("initial_margin_fraction", 0.40, 0.90),
        ("minimum_stop_atr", 0.30, 2.00),
        ("maximum_stop_atr", 0.60, 3.00),
        ("stop_buffer_atr", 0.0, 0.50),
        ("tp1_ratio", 0.10, 0.90),
        ("tp2_ratio", 0.10, 0.90),
        ("tp1_r_floor", 0.25, 1.50),
        ("tp1_r_cap", 0.40, 2.00),
        ("tp2_r_floor", 0.50, 2.50),
        ("tp2_r_cap", 0.75, 3.00),
        ("multi_timeframe_minimum_direction_score", 0.05, 0.60),
        ("multi_timeframe_strong_direction_score", 0.15, 0.90),
        ("orderflow_freshness_seconds", 15.0, 300.0),
        ("trend_maturity_extension_atr", 0.75, 6.0),
        ("router_minimum_net_ev_r", -0.25, 0.50),
        ("router_conflict_net_ev_gap_r", 0.02, 0.75),
        ("cross_sectional_minimum_score_gap", 0.0, 15.0),
        ("estimated_round_trip_fee_r", 0.0, 0.50),
        ("estimated_slippage_r", 0.0, 0.50),
        ("estimated_missed_fill_r", 0.0, 0.50),
        ("evidence_strong_score", 50.0, 95.0),
        ("evidence_elite_score", 50.0, 99.0),
        ("evidence_strong_percentile", 50.0, 99.0),
        ("evidence_elite_percentile", 50.0, 100.0),
        ("evidence_strong_speed_agreement", 0.0, 1.0),
        ("evidence_elite_speed_agreement", 0.0, 1.0),
        ("evidence_strong_persistence", 0.0, 1.0),
        ("evidence_elite_persistence", 0.0, 1.0),
        ("evidence_strong_margin_multiplier", 1.0, 1.50),
        ("evidence_elite_margin_multiplier", 1.0, 1.75),
        ("evidence_max_initial_margin_fraction", 0.40, 0.95),
        ("promotion_min_expectancy_r", -0.10, 1.00),
        ("promotion_min_profit_factor", 0.80, 3.00),
        ("promotion_min_calmar", 0.0, 5.00),
        ("promotion_max_pbo", 0.05, 0.95),
        ("promotion_max_symbol_concentration", 0.10, 1.00),
    ):
        normalized[key] = _bounded(
            normalized.get(key), lower, upper, float(defaults[key])
        )
    normalized["maximum_sweep_depth_atr"] = max(
        normalized["minimum_sweep_depth_atr"],
        normalized["maximum_sweep_depth_atr"],
    )
    normalized["maximum_stop_atr"] = max(
        normalized["minimum_stop_atr"], normalized["maximum_stop_atr"]
    )
    normalized["tp1_r_cap"] = max(
        normalized["tp1_r_floor"], normalized["tp1_r_cap"]
    )
    normalized["tp2_r_cap"] = max(
        normalized["tp2_r_floor"], normalized["tp2_r_cap"]
    )
    normalized["evidence_elite_score"] = max(
        normalized["evidence_strong_score"], normalized["evidence_elite_score"]
    )
    normalized["evidence_elite_percentile"] = max(
        normalized["evidence_strong_percentile"],
        normalized["evidence_elite_percentile"],
    )
    normalized["evidence_elite_speed_agreement"] = max(
        normalized["evidence_strong_speed_agreement"],
        normalized["evidence_elite_speed_agreement"],
    )
    normalized["evidence_elite_persistence"] = max(
        normalized["evidence_strong_persistence"],
        normalized["evidence_elite_persistence"],
    )
    normalized["evidence_elite_margin_multiplier"] = max(
        normalized["evidence_strong_margin_multiplier"],
        normalized["evidence_elite_margin_multiplier"],
    )
    ratio_sum = normalized["tp1_ratio"] + normalized["tp2_ratio"]
    normalized["tp1_ratio"] /= ratio_sum
    normalized["tp2_ratio"] = 1.0 - normalized["tp1_ratio"]
    normalized["risk_tier"] = "base"
    raw_weights = normalized.get("multi_timeframe_weights")
    raw_weights = raw_weights if isinstance(raw_weights, Mapping) else defaults["multi_timeframe_weights"]
    weights = {
        timeframe: max(0.0, float(_finite(raw_weights.get(timeframe), weight) or weight))
        for timeframe, weight in defaults["multi_timeframe_weights"].items()
    }
    weight_sum = sum(weights.values()) or 1.0
    normalized["multi_timeframe_weights"] = {
        timeframe: value / weight_sum for timeframe, value in weights.items()
    }
    raw_speed_weights = normalized.get("multi_speed_weights")
    raw_speed_weights = (
        raw_speed_weights
        if isinstance(raw_speed_weights, Mapping)
        else defaults["multi_speed_weights"]
    )
    speed_weights = {
        speed: max(
            0.0,
            float(_finite(raw_speed_weights.get(speed), weight) or weight),
        )
        for speed, weight in defaults["multi_speed_weights"].items()
    }
    speed_weight_sum = sum(speed_weights.values()) or 1.0
    normalized["multi_speed_weights"] = {
        speed: value / speed_weight_sum for speed, value in speed_weights.items()
    }
    normalized["profile_version"] = SMALL_ACCOUNT_REGIME_PROFILE_VERSION
    return normalized


def _clean_rows(rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for row in rows or ():
        open_price = _finite(row.get("open"))
        high = _finite(row.get("high"))
        low = _finite(row.get("low"))
        close = _finite(row.get("close"))
        volume = _finite(row.get("volume"), 0.0)
        if any(value is None for value in (open_price, high, low, close)):
            continue
        if high <= 0 or low <= 0 or close <= 0 or high < low:
            continue
        cleaned.append({
            "open": float(open_price),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": max(0.0, float(volume or 0.0)),
            "timestamp": row.get("timestamp"),
        })
    return cleaned


def _atr(rows: Sequence[Mapping[str, float]], period: int = 14) -> float | None:
    if len(rows) < 3:
        return None
    values: list[float] = []
    for index in range(1, len(rows)):
        previous_close = float(rows[index - 1]["close"])
        high = float(rows[index]["high"])
        low = float(rows[index]["low"])
        values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    window = values[-max(2, min(int(period), len(values))):]
    return sum(window) / len(window) if window else None


def _score_linear(value: float, lower: float, upper: float) -> float:
    if upper <= lower:
        return 50.0
    return max(0.0, min(100.0, (float(value) - lower) / (upper - lower) * 100.0))


def _market_regime_direction(regime_context: Mapping[str, Any] | None) -> tuple[str | None, int]:
    items = (regime_context or {}).get("items")
    if not isinstance(items, Mapping):
        return None, 0
    directions = [
        str(item.get("direction") or "").strip().lower()
        for item in items.values()
        if isinstance(item, Mapping)
    ]
    long_count = sum(value == "long" for value in directions)
    short_count = sum(value == "short" for value in directions)
    if long_count >= 2:
        return "long", long_count
    if short_count >= 2:
        return "short", short_count
    return None, max(long_count, short_count)


def _ema(values: Sequence[float], period: int) -> float:
    alpha = 2.0 / (max(1, int(period)) + 1.0)
    current = float(values[0])
    for value in values[1:]:
        current += alpha * (float(value) - current)
    return current


def _clamp_unit(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _trend_speed_score(
    closes: Sequence[float],
    atr_value: float,
    *,
    fast_period: int,
    slow_period: int,
    momentum_lookback: int,
    slope_lag: int,
) -> float:
    """Score one trend speed using price, cross, slope and momentum evidence."""

    close = float(closes[-1])
    atr_floor = max(float(atr_value), close * 1e-6)
    fast = _ema(closes, fast_period)
    slow = _ema(closes, slow_period)
    previous = closes[:-max(1, int(slope_lag))]
    previous_slow = _ema(previous, slow_period) if previous else slow
    lookback = min(max(1, int(momentum_lookback)), len(closes) - 1)
    momentum_scale = max(2.0, min(6.0, lookback / 4.0))
    return (
        _clamp_unit((close - slow) / (2.0 * atr_floor)) * 0.25
        + _clamp_unit((fast - slow) / atr_floor) * 0.35
        + _clamp_unit((slow - previous_slow) / atr_floor) * 0.20
        + _clamp_unit(
            (close - float(closes[-1 - lookback]))
            / (momentum_scale * atr_floor)
        )
        * 0.20
    )


def _multi_speed_snapshot(
    closes: Sequence[float],
    atr_value: float,
    speed_weights: Mapping[str, float],
) -> dict[str, Any]:
    definitions = {
        "fast": (8, 21, 4, 2),
        "medium": (20, 50, 12, 3),
        "slow": (50, 100, 24, 5),
    }
    scores = {
        speed: _trend_speed_score(
            closes,
            atr_value,
            fast_period=definition[0],
            slow_period=definition[1],
            momentum_lookback=definition[2],
            slope_lag=definition[3],
        )
        for speed, definition in definitions.items()
    }
    directions = {
        speed: (
            "long" if score > 0.12 else "short" if score < -0.12 else "neutral"
        )
        for speed, score in scores.items()
    }
    blended = sum(float(speed_weights[speed]) * score for speed, score in scores.items())
    direction = "long" if blended > 0.12 else "short" if blended < -0.12 else "neutral"
    agreement = (
        sum(
            float(speed_weights[speed])
            for speed, value in directions.items()
            if value == direction
        )
        if direction in {"long", "short"}
        else 0.0
    )
    return {
        "score": blended,
        "direction": direction,
        "agreement": agreement,
        "scores": scores,
        "directions": directions,
    }


def _timeframe_trend_snapshot(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    maturity_extension_atr: float,
    maturity_run_bars: int,
    speed_weights: Mapping[str, float],
    persistence_lag_bars: int,
) -> dict[str, Any]:
    candles = _clean_rows(rows)
    if len(candles) < 70:
        return {
            "available": False,
            "direction": "unknown",
            "direction_score": 0.0,
            "reason": f"data {len(candles)}/70",
        }
    closes = [row["close"] for row in candles]
    atr_value = _atr(candles, 14)
    if atr_value is None or atr_value <= 0:
        return {
            "available": False,
            "direction": "unknown",
            "direction_score": 0.0,
            "reason": "ATR unavailable",
        }
    close = closes[-1]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    atr_floor = max(atr_value, close * 1e-6)
    current = _multi_speed_snapshot(closes, atr_value, speed_weights)
    lag = max(2, min(int(persistence_lag_bars), len(closes) - 60))
    previous_closes = closes[:-lag]
    previous_rows = candles[:-lag]
    previous_atr = _atr(previous_rows, 14) or atr_value
    previous = _multi_speed_snapshot(previous_closes, previous_atr, speed_weights)
    direction_score = float(current["score"])
    direction = str(current["direction"])
    previous_direction = str(previous["direction"])
    if direction in {"long", "short"} and previous_direction == direction:
        persistence_score = min(
            1.0,
            0.35
            + 0.35 * float(current["agreement"])
            + 0.30 * min(1.0, abs(float(previous["score"]))),
        )
    elif direction in {"long", "short"} and previous_direction == "neutral":
        persistence_score = 0.30
    else:
        persistence_score = 0.0
    run_bars = 0
    if direction in {"long", "short"}:
        sign = 1.0 if direction == "long" else -1.0
        for value in reversed(closes):
            if sign * (value - ema50) <= 0:
                break
            run_bars += 1
    extension_atr = abs(close - ema20) / atr_floor
    mature = bool(
        direction in {"long", "short"}
        and run_bars >= int(maturity_run_bars)
        and extension_atr >= float(maturity_extension_atr)
    )
    return {
        "available": True,
        "direction": direction,
        "direction_score": round(direction_score, 6),
        "previous_direction": previous_direction,
        "previous_direction_score": round(float(previous["score"]), 6),
        "transition": f"{previous_direction}_to_{direction}",
        "state_persistent": bool(
            direction in {"long", "short"} and previous_direction == direction
        ),
        "persistence_score": round(persistence_score, 6),
        "multi_speed_agreement": round(float(current["agreement"]), 6),
        "speed_scores": {
            key: round(float(value), 6)
            for key, value in current["scores"].items()
        },
        "speed_directions": dict(current["directions"]),
        "close": close,
        "ema20": ema20,
        "ema50": ema50,
        "atr": atr_value,
        "extension_atr": round(extension_atr, 6),
        "run_bars": run_bars,
        "mature": mature,
    }


def evaluate_multi_timeframe_regime(
    timeframe_rows: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Blend 15m/1h/4h/1d trend state without an all-timeframe AND gate."""

    cfg = normalize_small_account_regime_config(config)
    weights = cfg["multi_timeframe_weights"]
    snapshots: dict[str, dict[str, Any]] = {}
    weighted_sum = 0.0
    available_weight = 0.0
    directions: list[str] = []
    for timeframe, weight in weights.items():
        snapshot = _timeframe_trend_snapshot(
            (timeframe_rows or {}).get(timeframe),
            maturity_extension_atr=cfg["trend_maturity_extension_atr"],
            maturity_run_bars=int(cfg["trend_maturity_run_bars"]),
            speed_weights=cfg["multi_speed_weights"],
            persistence_lag_bars=int(cfg["state_persistence_lag_bars"]),
        )
        snapshots[timeframe] = snapshot
        if not snapshot.get("available"):
            continue
        weighted_sum += float(weight) * float(snapshot.get("direction_score", 0.0) or 0.0)
        available_weight += float(weight)
        if snapshot.get("direction") in {"long", "short"}:
            directions.append(str(snapshot["direction"]))
    score = weighted_sum / available_weight if available_weight > 0 else 0.0
    long_votes = sum(value == "long" for value in directions)
    short_votes = sum(value == "short" for value in directions)
    disagreements = min(long_votes, short_votes)
    minimum = float(cfg["multi_timeframe_minimum_direction_score"])
    regime = "up" if score >= minimum else "down" if score <= -minimum else "range"
    mature_timeframes = [
        timeframe for timeframe, snapshot in snapshots.items() if snapshot.get("mature")
    ]
    speed_agreement = (
        sum(
            float(weights[timeframe])
            * float(snapshot.get("multi_speed_agreement", 0.0) or 0.0)
            for timeframe, snapshot in snapshots.items()
            if snapshot.get("available")
        )
        / available_weight
        if available_weight > 0
        else 0.0
    )
    overall_direction = (
        "long" if score >= minimum else "short" if score <= -minimum else "neutral"
    )
    directional_weight = sum(
        float(weights[timeframe])
        for timeframe, snapshot in snapshots.items()
        if snapshot.get("available")
        and snapshot.get("direction") == overall_direction
    )
    persistence_score = (
        sum(
            float(weights[timeframe])
            * float(snapshot.get("persistence_score", 0.0) or 0.0)
            for timeframe, snapshot in snapshots.items()
            if snapshot.get("available")
            and snapshot.get("direction") == overall_direction
        )
        / directional_weight
        if directional_weight > 0
        else 0.0
    )
    persistent_weight = sum(
        float(weights[timeframe])
        for timeframe, snapshot in snapshots.items()
        if snapshot.get("available")
        and snapshot.get("state_persistent")
        and snapshot.get("direction") == overall_direction
    )
    transition = (
        "persistent_up"
        if score >= minimum and persistent_weight >= 0.50
        else "persistent_down"
        if score <= -minimum and persistent_weight >= 0.50
        else "transition_or_mixed"
    )
    return {
        "profile": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
        "available": available_weight >= 0.50,
        "weighted_direction_score": round(score, 6),
        "direction": overall_direction,
        "regime": regime,
        "disagreements": disagreements,
        "ambiguous": bool(
            abs(score) < minimum
            or disagreements > int(cfg["multi_timeframe_max_disagreements"])
        ),
        "mature": bool(mature_timeframes),
        "mature_timeframes": mature_timeframes,
        "multi_speed_agreement": round(speed_agreement, 6),
        "persistence_score": round(persistence_score, 6),
        "persistent_weight": round(persistent_weight, 6),
        "transition": transition,
        "snapshots": snapshots,
    }


def _multi_timeframe_fitness(
    side: Any,
    context: Mapping[str, Any] | None,
) -> float:
    direction = 1.0 if str(side or "").lower() == "long" else -1.0
    score = float(_finite((context or {}).get("weighted_direction_score"), 0.0) or 0.0)
    fitness = max(0.0, min(100.0, 50.0 + direction * score * 50.0))
    if bool((context or {}).get("mature")) and direction * score > 0:
        fitness = max(0.0, fitness - 12.0)
    return fitness


def evaluate_small_account_exhaustion_reversal(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    trend_metrics: Mapping[str, Any] | None = None,
    futures_context: Mapping[str, Any] | None = None,
    market_regime_context: Mapping[str, Any] | None = None,
    multi_timeframe_context: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    tradfi: bool = False,
) -> dict[str, Any]:
    """Return a confirmed reversal candidate without overriding a trend entry."""

    cfg = normalize_small_account_regime_config(config)
    result: dict[str, Any] = {
        "allowed": False,
        "side": None,
        "score": 0.0,
        "source": "exhaustion_reversal",
        "agreement": "reversal_only",
        "code": "NO_EXHAUSTION_REVERSAL",
        "reason": "no confirmed exhaustion sweep/reclaim",
        "profile": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
        "risk_tier": "base",
        "initial_margin_fraction": cfg["initial_margin_fraction"],
    }
    if not cfg["enabled"]:
        result.update(code="REGIME_ENSEMBLE_DISABLED", reason="regime ensemble disabled")
        return result
    if tradfi and cfg["crypto_only"]:
        result.update(
            code="REGIME_REVERSAL_CRYPTO_ONLY",
            reason="exhaustion reversal is intentionally disabled for TradFi entries",
        )
        return result

    candles = _clean_rows(rows)
    lookback = int(cfg["sweep_lookback_bars"])
    volume_lookback = int(cfg["volume_lookback_bars"])
    required = max(lookback + 2, volume_lookback + 2, 18)
    if len(candles) < required:
        result.update(code="REGIME_REVERSAL_DATA_SHORT", reason="insufficient completed candles")
        return result
    atr_value = _atr(candles, 14)
    if atr_value is None or atr_value <= 0:
        result.update(code="REGIME_REVERSAL_ATR_UNAVAILABLE", reason="event ATR unavailable")
        return result

    latest = candles[-1]
    prior = candles[-lookback - 1:-1]
    prior_low = min(row["low"] for row in prior)
    prior_high = max(row["high"] for row in prior)
    candle_range = max(latest["high"] - latest["low"], 1e-12)
    lower_wick = max(0.0, min(latest["open"], latest["close"]) - latest["low"]) / candle_range
    upper_wick = max(0.0, latest["high"] - max(latest["open"], latest["close"])) / candle_range
    long_close_location = (latest["close"] - latest["low"]) / candle_range
    short_close_location = (latest["high"] - latest["close"]) / candle_range
    long_depth_atr = max(0.0, prior_low - latest["low"]) / atr_value
    short_depth_atr = max(0.0, latest["high"] - prior_high) / atr_value
    long_price_confirmed = bool(
        latest["low"] < prior_low
        and latest["close"] > prior_low
        and lower_wick >= cfg["minimum_wick_ratio"]
        and long_close_location >= cfg["minimum_reclaim_close_location"]
        and cfg["minimum_sweep_depth_atr"] <= long_depth_atr <= cfg["maximum_sweep_depth_atr"]
    )
    short_price_confirmed = bool(
        latest["high"] > prior_high
        and latest["close"] < prior_high
        and upper_wick >= cfg["minimum_wick_ratio"]
        and short_close_location >= cfg["minimum_reclaim_close_location"]
        and cfg["minimum_sweep_depth_atr"] <= short_depth_atr <= cfg["maximum_sweep_depth_atr"]
    )
    if not long_price_confirmed and not short_price_confirmed:
        result.update({
            "atr": atr_value,
            "prior_low": prior_low,
            "prior_high": prior_high,
            "long_depth_atr": long_depth_atr,
            "short_depth_atr": short_depth_atr,
        })
        return result

    baseline_volume = median(row["volume"] for row in candles[-volume_lookback - 1:-1])
    volume_ratio = latest["volume"] / max(baseline_volume, 1e-9) if baseline_volume > 0 else 1.0
    trend = dict(trend_metrics or {})
    context = dict(futures_context or {})
    weighted_momentum = float(_finite(trend.get("weighted_momentum"), 0.0) or 0.0)
    broad_side = "long" if weighted_momentum > 0 else "short" if weighted_momentum < 0 else None
    horizon_votes = trend.get("horizon_votes") if isinstance(trend.get("horizon_votes"), Mapping) else {}
    trend_clarity = float(_finite(trend.get("trend_clarity"), 0.0) or 0.0)
    market_side, market_votes = _market_regime_direction(market_regime_context)
    multi_context = dict(multi_timeframe_context or {})
    snapshots = (
        multi_context.get("snapshots")
        if isinstance(multi_context.get("snapshots"), Mapping)
        else {}
    )
    recent = candles[-volume_lookback:]
    volume_sum = sum(row["volume"] for row in recent)
    vwap = (
        sum(((row["high"] + row["low"] + row["close"]) / 3.0) * row["volume"] for row in recent)
        / max(volume_sum, 1e-9)
        if volume_sum > 0
        else latest["close"]
    )

    candidates: list[dict[str, Any]] = []
    for side, price_confirmed, wick_ratio, close_location, depth_atr, structure_stop in (
        ("long", long_price_confirmed, lower_wick, long_close_location, long_depth_atr, latest["low"]),
        ("short", short_price_confirmed, upper_wick, short_close_location, short_depth_atr, latest["high"]),
    ):
        if not price_confirmed:
            continue
        opposite_votes = sum(value == broad_side for value in horizon_votes.values())
        countertrend = broad_side in {"long", "short"} and broad_side != side
        if countertrend and (
            abs(weighted_momentum) >= cfg["strong_opposite_momentum_block"]
            or opposite_votes >= int(cfg["strong_opposite_vote_block"])
            or trend_clarity >= cfg["maximum_countertrend_clarity"]
        ):
            continue
        if market_side in {"long", "short"} and market_side != side and market_votes >= 2:
            continue
        opposite = "short" if side == "long" else "long"
        higher_directions = [
            str((snapshots.get(timeframe) or {}).get("direction") or "unknown")
            for timeframe in ("4h", "1d")
        ]
        higher_scores = [
            abs(float(_finite((snapshots.get(timeframe) or {}).get("direction_score"), 0.0) or 0.0))
            for timeframe in ("4h", "1d")
        ]
        if (
            higher_directions == [opposite, opposite]
            and min(higher_scores or [0.0])
            >= float(cfg["multi_timeframe_strong_direction_score"])
        ):
            continue

        vwap_extension_atr = (
            (vwap - latest["low"]) / atr_value
            if side == "long"
            else (latest["high"] - vwap) / atr_value
        )
        range_extension_atr = long_depth_atr if side == "long" else short_depth_atr
        if (
            vwap_extension_atr < cfg["minimum_vwap_extension_atr"]
            or range_extension_atr < cfg["minimum_range_extension_atr"]
        ):
            continue

        direction = 1.0 if side == "long" else -1.0
        ofi = _finite(context.get("rolling_orderbook_imbalance_pct"))
        taker = _finite(context.get("taker_buy_sell_ratio"))
        funding = _finite(context.get("funding_rate"))
        funding_pct = max(
            float(_finite(context.get("funding_percentile_7d"), 0.0) or 0.0),
            float(_finite(context.get("funding_percentile_30d"), 0.0) or 0.0),
        )
        long_short = _finite(context.get("long_short_ratio"))
        oi_z = _finite(context.get("open_interest_delta_z"))
        orderflow_age = _finite(context.get("orderflow_age_seconds"))
        orderflow_fresh = orderflow_age is None or orderflow_age <= 90.0

        orderflow_confirmed = bool(
            orderflow_fresh
            and (
                (ofi is not None and direction * ofi >= cfg["orderflow_imbalance_confirm_pct"])
                or (
                    taker is not None
                    and direction * (taker - 1.0) >= cfg["taker_ratio_confirm_delta"]
                )
            )
        )
        crowding_confirmed = bool(
            funding is not None
            and funding_pct >= cfg["funding_percentile_confirm"]
            and ((side == "short" and funding > 0) or (side == "long" and funding < 0))
        )
        ratio_confirmed = bool(
            long_short is not None
            and (
                (side == "short" and long_short >= cfg["long_short_ratio_short_confirm"])
                or (side == "long" and long_short <= cfg["long_short_ratio_long_confirm"])
            )
        )
        oi_confirmed = bool(oi_z is not None and oi_z >= cfg["open_interest_z_confirm"])
        volume_confirmed = volume_ratio >= cfg["minimum_volume_ratio"]
        nonprice_confirmations = sum(
            (volume_confirmed, orderflow_confirmed, crowding_confirmed, ratio_confirmed, oi_confirmed)
        )
        if nonprice_confirmations < int(cfg["minimum_nonprice_confirmations"]):
            continue

        price_score = (
            _score_linear(wick_ratio, cfg["minimum_wick_ratio"], 0.70) * 0.35
            + _score_linear(close_location, cfg["minimum_reclaim_close_location"], 0.95) * 0.35
            + _score_linear(depth_atr, cfg["minimum_sweep_depth_atr"], 0.75) * 0.30
        )
        regime_score = max(0.0, min(100.0, 100.0 - trend_clarity * 100.0))
        if broad_side == side:
            regime_score = min(100.0, regime_score + 18.0)
        participation_score = min(100.0, 45.0 + max(0.0, volume_ratio - 1.0) * 55.0)
        confirmation_score = min(100.0, 35.0 + nonprice_confirmations * 16.0)
        score = (
            price_score * 0.45
            + regime_score * 0.20
            + participation_score * 0.15
            + confirmation_score * 0.20
        )
        if score < cfg["minimum_score"]:
            continue

        stop_distance_atr = max(
            cfg["minimum_stop_atr"],
            min(cfg["maximum_stop_atr"], depth_atr + cfg["stop_buffer_atr"]),
        )
        risk_distance = stop_distance_atr * atr_value
        range_mid = (prior_low + prior_high) / 2.0
        directional_targets = (
            [value for value in (vwap, range_mid) if value > latest["close"]]
            if side == "long"
            else [value for value in (vwap, range_mid) if value < latest["close"]]
        )
        mean_target = (
            min(directional_targets)
            if side == "long" and directional_targets
            else max(directional_targets)
            if side == "short" and directional_targets
            else latest["close"] + direction * risk_distance * cfg["tp2_r_floor"]
        )
        target_r = abs(mean_target - latest["close"]) / max(risk_distance, 1e-12)
        tp2_r = max(cfg["tp2_r_floor"], min(cfg["tp2_r_cap"], target_r))
        tp1_r = max(
            cfg["tp1_r_floor"],
            min(cfg["tp1_r_cap"], tp2_r * 0.60),
        )
        candidates.append({
            "allowed": True,
            "side": side,
            "score": round(score, 2),
            "source": "exhaustion_reversal",
            "agreement": "reversal_only",
            "code": f"SMALL_ACCOUNT_EXHAUSTION_REVERSAL_{side.upper()}",
            "reason": (
                f"confirmed {side} sweep/reclaim: score={score:.1f}, "
                f"volume={volume_ratio:.2f}x, confirmations={nonprice_confirmations}"
            ),
            "profile": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
            "risk_tier": "base",
            "initial_margin_fraction": cfg["initial_margin_fraction"],
            "reference_price": latest["close"],
            "atr": atr_value,
            "signal_candle_ts": latest.get("timestamp"),
            "structure_stop": structure_stop,
            "event_structure_stop": structure_stop,
            "stop_atr_multiplier": stop_distance_atr,
            "reversal_mean_target_price": mean_target,
            "reversal_tp1_r": tp1_r,
            "reversal_tp2_r": tp2_r,
            "volume_ratio": volume_ratio,
            "wick_ratio": wick_ratio,
            "close_location": close_location,
            "sweep_depth_atr": depth_atr,
            "vwap": vwap,
            "vwap_extension_atr": vwap_extension_atr,
            "weighted_momentum": weighted_momentum,
            "trend_clarity": trend_clarity,
            "broad_side": broad_side,
            "market_side": market_side,
            "nonprice_confirmations": nonprice_confirmations,
            "orderflow_confirmed": orderflow_confirmed,
            "crowding_confirmed": crowding_confirmed,
            "ratio_confirmed": ratio_confirmed,
            "oi_confirmed": oi_confirmed,
            "price_score": price_score,
            "regime_score": regime_score,
            "multi_timeframe_regime": multi_context.get("regime"),
            "multi_timeframe_direction_score": multi_context.get(
                "weighted_direction_score"
            ),
        })

    if not candidates:
        result.update({
            "code": "REGIME_REVERSAL_BLOCKED_OR_UNCONFIRMED",
            "reason": "sweep was not confirmed by regime/participation context",
            "weighted_momentum": weighted_momentum,
            "trend_clarity": trend_clarity,
            "market_side": market_side,
            "volume_ratio": volume_ratio,
        })
        return result
    return max(candidates, key=lambda item: float(item["score"]))


def _candidate_net_ev(
    candidate: Mapping[str, Any],
    *,
    engine: str,
    multi_timeframe_context: Mapping[str, Any] | None,
    cost_context: Mapping[str, Any] | None,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    score = max(0.0, min(100.0, float(_finite(candidate.get("score"), 0.0) or 0.0)))
    probability = max(0.44, min(0.70, 0.42 + score * 0.0027))
    if engine == "exhaustion_reversal":
        tp1_r = float(_finite(candidate.get("reversal_tp1_r"), 0.65) or 0.65)
        tp2_r = float(_finite(candidate.get("reversal_tp2_r"), 1.20) or 1.20)
        reward_r = config["tp1_ratio"] * tp1_r + config["tp2_ratio"] * tp2_r
    elif engine == "change_point_flow":
        reward_r = 1.90
    else:
        reward_r = 2.60
    fitness = _multi_timeframe_fitness(candidate.get("side"), multi_timeframe_context)
    timeframe_adjustment = (fitness - 50.0) / 500.0
    probability += timeframe_adjustment
    side_sign = 1.0 if str(candidate.get("side") or "").lower() == "long" else -1.0
    mtf_direction = str((multi_timeframe_context or {}).get("direction") or "neutral")
    mtf_available = bool((multi_timeframe_context or {}).get("available"))
    aligned_with_mtf = mtf_direction == str(candidate.get("side") or "").lower()
    speed_agreement = float(
        _finite((multi_timeframe_context or {}).get("multi_speed_agreement"), 0.0)
        or 0.0
    )
    persistence = float(
        _finite((multi_timeframe_context or {}).get("persistence_score"), 0.0)
        or 0.0
    )
    transition = str(
        (multi_timeframe_context or {}).get("transition") or "transition_or_mixed"
    )
    speed_adjustment = 0.0 if not mtf_available else (
        max(-0.018, min(0.018, (speed_agreement - 0.50) * 0.045))
        if aligned_with_mtf
        else -min(0.020, speed_agreement * 0.025)
        if mtf_direction in {"long", "short"}
        else 0.0
    )
    persistence_adjustment = 0.0
    if (
        mtf_available
        and aligned_with_mtf
        and str(candidate.get("side") or "").lower() == "long"
        and transition == "persistent_up"
    ):
        persistence_adjustment = min(0.025, persistence * 0.030)
    elif (
        mtf_available
        and engine == "trend_continuation"
        and transition != "persistent_up"
    ):
        persistence_adjustment = -0.015

    context = cost_context or {}
    # The futures/spot basis is defined as (mark-index)/index by this bot.
    # Therefore a negative signed basis is the paper's favorable spot premium
    # for the entry direction. Keep the adjustment deliberately small because
    # basis and crowding are also used by downstream safety filters.
    basis = _finite(context.get("basis_pct"))
    basis_adjustment = (
        max(-0.012, min(0.012, -(side_sign * float(basis)) / 0.40 * 0.012))
        if basis is not None
        else 0.0
    )
    orderflow_adjustment = 0.0
    orderflow_age = _finite(context.get("orderflow_age_seconds"))
    if (
        orderflow_age is not None
        and orderflow_age <= float(config["orderflow_freshness_seconds"])
    ):
        imbalance = _finite(context.get("rolling_orderbook_imbalance_pct"))
        taker_ratio = _finite(context.get("taker_buy_sell_ratio"))
        if imbalance is not None:
            orderflow_adjustment += max(
                -0.012,
                min(0.012, side_sign * float(imbalance) / 20.0 * 0.012),
            )
        if taker_ratio is not None and taker_ratio > 0:
            orderflow_adjustment += max(
                -0.008,
                min(0.008, side_sign * (float(taker_ratio) - 1.0) / 0.20 * 0.008),
            )
        orderflow_adjustment = max(-0.020, min(0.020, orderflow_adjustment))
    probability = max(
        0.42,
        min(
            0.74,
            probability
            + speed_adjustment
            + persistence_adjustment
            + basis_adjustment
            + orderflow_adjustment,
        ),
    )
    costs = (
        float(config["estimated_round_trip_fee_r"])
        + float(config["estimated_slippage_r"])
        + float(config["estimated_missed_fill_r"])
    )
    spread_pct = float(_finite((cost_context or {}).get("futures_spread_pct"), 0.0) or 0.0)
    costs += min(0.20, max(0.0, spread_pct) / 2.0)
    funding = float(_finite((cost_context or {}).get("funding_rate"), 0.0) or 0.0)
    costs += min(0.12, max(0.0, side_sign * funding) * 40.0)
    net_ev = probability * reward_r - (1.0 - probability) - costs
    return {
        "win_probability": round(probability, 6),
        "gross_reward_r": round(reward_r, 6),
        "estimated_cost_r": round(costs, 6),
        "multi_timeframe_fitness": round(fitness, 4),
        "probability_basis": "bounded_score_plus_evidence_not_calibrated_forecast",
        "probability_adjustments": {
            "timeframe": round(timeframe_adjustment, 6),
            "multi_speed": round(speed_adjustment, 6),
            "state_persistence": round(persistence_adjustment, 6),
            "basis": round(basis_adjustment, 6),
            "fresh_orderflow": round(orderflow_adjustment, 6),
        },
        "regime_transition": transition,
        "multi_speed_agreement": round(speed_agreement, 6),
        "persistence_score": round(persistence, 6),
        "orderflow_age_seconds": orderflow_age,
        "net_ev_r": round(net_ev, 6),
    }


def resolve_small_account_evidence_allocation(
    candidate_resolution: Mapping[str, Any] | None,
    multi_timeframe_context: Mapping[str, Any] | None,
    selector_candidate: Mapping[str, Any] | None,
    *,
    risk_tier: Any,
    initial_margin_fraction: Any,
    tradfi: bool = False,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Scale first-stage margin once for corroborated crypto momentum.

    This is deliberately an upside-only allocator: an ordinary signal keeps
    the configured aggressive fraction, while a research-aligned persistent
    crypto long may use more of the already-budgeted margin.  Volatility,
    stop-distance, liquidation and per-position loss limits remain downstream
    authorities and are not multiplied here.
    """

    cfg = normalize_small_account_regime_config(config)
    candidate = dict(candidate_resolution or {})
    mtf = dict(multi_timeframe_context or {})
    selector = dict(selector_candidate or {})
    base_fraction = _bounded(initial_margin_fraction, 0.40, 1.00, 0.65)
    tier = str(risk_tier or "base").strip().lower()
    if tier not in {"base", "strong", "elite"}:
        tier = "base"
    side = str(candidate.get("side") or "").strip().lower()
    source = str(candidate.get("source") or "").strip().lower()
    engine = str(candidate.get("regime_engine") or "").strip().lower()
    score = float(
        _finite(
            selector.get("convex_rotation_score"),
            _finite(candidate.get("score"), 0.0),
        )
        or 0.0
    )
    percentile = float(
        _finite(selector.get("convex_rotation_percentile"), 0.0) or 0.0
    )
    universe_size = int(
        max(0.0, float(_finite(selector.get("convex_rotation_universe_size"), 0.0) or 0.0))
    )
    speed_agreement = float(
        _finite(mtf.get("multi_speed_agreement"), 0.0) or 0.0
    )
    persistence = float(_finite(mtf.get("persistence_score"), 0.0) or 0.0)
    transition = str(mtf.get("transition") or "transition_or_mixed")
    mtf_side = str(mtf.get("direction") or "neutral").strip().lower()
    selected_edge = (
        candidate.get("selected_net_edge")
        if isinstance(candidate.get("selected_net_edge"), Mapping)
        else {}
    )
    adjustments = (
        selected_edge.get("probability_adjustments")
        if isinstance(selected_edge.get("probability_adjustments"), Mapping)
        else {}
    )
    orderflow_adjustment = float(
        _finite(adjustments.get("fresh_orderflow"), 0.0) or 0.0
    )
    basis_adjustment = float(_finite(adjustments.get("basis"), 0.0) or 0.0)
    orderflow_age = _finite(selected_edge.get("orderflow_age_seconds"))
    fresh_supportive_flow = bool(
        orderflow_age is not None
        and orderflow_age <= float(cfg["orderflow_freshness_seconds"])
        and orderflow_adjustment > 0.0
    )
    common_conditions = {
        "enabled": bool(cfg["evidence_allocation_enabled"]),
        "crypto": not bool(tradfi),
        "trend_path": source in {"trend_only", "aligned"}
        and engine in {"", "trend_continuation"},
        "long": side == "long",
        "fresh_continuation": bool(candidate.get("fresh_continuation")),
        "persistent_up": transition == "persistent_up"
        and mtf_side == "long"
        and bool(mtf.get("available")),
        "cross_section_available": universe_size
        >= int(cfg["evidence_minimum_universe_size"]),
        "fresh_supportive_orderflow": fresh_supportive_flow,
        "basis_not_adverse": basis_adjustment >= -0.003,
    }
    evidence_tier = "base"
    multiplier = 1.0
    strong_conditions = {
        "risk_tier": tier in {"strong", "elite"},
        "score": score >= float(cfg["evidence_strong_score"]),
        "percentile": percentile >= float(cfg["evidence_strong_percentile"]),
        "multi_speed": speed_agreement
        >= float(cfg["evidence_strong_speed_agreement"]),
        "persistence": persistence >= float(cfg["evidence_strong_persistence"]),
    }
    elite_conditions = {
        "risk_tier": tier == "elite",
        "score": score >= float(cfg["evidence_elite_score"]),
        "percentile": percentile >= float(cfg["evidence_elite_percentile"]),
        "multi_speed": speed_agreement
        >= float(cfg["evidence_elite_speed_agreement"]),
        "persistence": persistence >= float(cfg["evidence_elite_persistence"]),
    }
    if all(common_conditions.values()) and all(strong_conditions.values()):
        evidence_tier = "strong"
        multiplier = float(cfg["evidence_strong_margin_multiplier"])
        if all(elite_conditions.values()):
            evidence_tier = "elite"
            multiplier = float(cfg["evidence_elite_margin_multiplier"])
    max_fraction = float(cfg["evidence_max_initial_margin_fraction"])
    adjusted_fraction = min(max_fraction, base_fraction * multiplier)
    applied = adjusted_fraction > base_fraction + 1e-12
    unmet = [
        name
        for name, passed in {**common_conditions, **strong_conditions}.items()
        if not passed
    ]
    reason = (
        f"{evidence_tier} crypto risk-managed momentum allocation: "
        f"margin {base_fraction:.3f}->{adjusted_fraction:.3f}"
        if applied
        else "base aggressive allocation retained"
        + (f"; unmet={','.join(unmet)}" if unmet else "")
    )
    return {
        "profile": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
        "applied": applied,
        "evidence_tier": evidence_tier,
        "margin_multiplier": round(multiplier, 6),
        "base_initial_margin_fraction": round(base_fraction, 6),
        "initial_margin_fraction": round(adjusted_fraction, 6),
        "reason": reason,
        "evidence": {
            "side": side,
            "source": source,
            "risk_tier": tier,
            "score": round(score, 6),
            "percentile": round(percentile, 6),
            "universe_size": universe_size,
            "transition": transition,
            "multi_speed_agreement": round(speed_agreement, 6),
            "persistence_score": round(persistence, 6),
            "orderflow_age_seconds": orderflow_age,
            "fresh_orderflow_adjustment": round(orderflow_adjustment, 6),
            "basis_adjustment": round(basis_adjustment, 6),
            "conditions": {**common_conditions, **strong_conditions},
        },
    }


def evaluate_regime_challenger_promotion(
    events: Sequence[Mapping[str, Any]] | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the PDF's independent-sample, cost, walk-forward and PBO gate."""

    cfg = normalize_small_account_regime_config(config)
    unique: dict[str, dict[str, Any]] = {}
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or "") != "shadow_outcome":
            continue
        if str(event.get("shadow_engine") or "") != "exhaustion_reversal":
            continue
        key = str(event.get("shadow_key") or "").strip()
        if not key:
            continue
        raw_r = _finite(event.get("pnl_r"))
        if raw_r is None:
            continue
        cost_r = max(0.0, float(_finite(event.get("estimated_cost_r"), 0.0) or 0.0))
        net_r = float(raw_r) - cost_r
        unique[key] = {
            "pnl_r": net_r,
            "pnl_usdt": net_r,
            "return_pct": net_r,
            "timestamp": event.get("shadow_exit_ts") or event.get("ts"),
            "exit_ts": event.get("shadow_exit_ts") or event.get("ts"),
            "symbol": str(event.get("symbol") or "unknown"),
            "regime": str(event.get("shadow_regime") or "unknown"),
            "mfe_r": event.get("mfe_r"),
            "mae_r": event.get("mae_r"),
        }
    trades = list(unique.values())
    sample_count = len(trades)
    regime_counts = Counter(row["regime"] for row in trades)
    symbol_counts = Counter(row["symbol"] for row in trades)
    maximum_symbol_concentration = (
        max(symbol_counts.values()) / sample_count if sample_count else 1.0
    )
    # Import locally so the intelligence layer stays independent and this pure
    # module does not create an import cycle at process startup.
    from .intelligence import run_overfit_backtest
    from .performance import calculate_performance_metrics

    validation_cfg = {
        "overfit_min_samples": int(cfg["promotion_min_trades"]),
        "overfit_min_profit_factor": float(cfg["promotion_min_profit_factor"]),
        "overfit_max_pbo": float(cfg["promotion_max_pbo"]),
        "overfit_multiple_testing_trials": int(cfg["promotion_multiple_testing_trials"]),
        "overfit_walk_forward_train_size": 50,
        "overfit_walk_forward_test_size": 20,
        "overfit_walk_forward_purge_size": 2,
        "overfit_walk_forward_embargo_size": 2,
        "overfit_min_oos_windows": 2,
    }
    report = run_overfit_backtest(
        trades,
        validation_cfg,
        number_of_trials=int(cfg["promotion_multiple_testing_trials"]),
    )
    metrics = calculate_performance_metrics(trades, initial_equity_usdt=100.0)
    required_regimes = ("up", "down", "range")
    reasons: list[str] = []
    if sample_count < int(cfg["promotion_min_trades"]):
        reasons.append(f"samples {sample_count}/{int(cfg['promotion_min_trades'])}")
    for regime in required_regimes:
        count = int(regime_counts.get(regime, 0))
        if count < int(cfg["promotion_min_regime_trades"]):
            reasons.append(
                f"{regime} regime samples {count}/{int(cfg['promotion_min_regime_trades'])}"
            )
    if float(metrics.expectancy_r) < float(cfg["promotion_min_expectancy_r"]):
        reasons.append(
            f"expectancy {metrics.expectancy_r:.3f}R<{float(cfg['promotion_min_expectancy_r']):.3f}R"
        )
    if float(metrics.profit_factor) < float(cfg["promotion_min_profit_factor"]):
        reasons.append(
            f"profit factor {metrics.profit_factor:.2f}<{float(cfg['promotion_min_profit_factor']):.2f}"
        )
    calmar = float(metrics.calmar_ratio or 0.0)
    if calmar < float(cfg["promotion_min_calmar"]):
        reasons.append(f"Calmar {calmar:.2f}<{float(cfg['promotion_min_calmar']):.2f}")
    if maximum_symbol_concentration > float(cfg["promotion_max_symbol_concentration"]):
        reasons.append(
            f"symbol concentration {maximum_symbol_concentration:.1%}>"
            f"{float(cfg['promotion_max_symbol_concentration']):.1%}"
        )
    if not report.passed:
        reasons.extend(f"validation: {reason}" for reason in report.reasons)
    qualified = not reasons
    live_allowed = bool(
        qualified
        and (
            not cfg["promotion_required"]
            or cfg["auto_promote_when_qualified"]
        )
    )
    return {
        "qualified": qualified,
        "live_allowed": live_allowed,
        "sample_count": sample_count,
        "minimum_samples": int(cfg["promotion_min_trades"]),
        "regime_counts": dict(regime_counts),
        "expectancy_r": float(metrics.expectancy_r),
        "profit_factor": float(metrics.profit_factor),
        "calmar_ratio": calmar,
        "pbo": float(report.pbo),
        "deflated_sharpe_pass": bool(report.deflated_sharpe_pass),
        "adjusted_expectancy_r": float(report.adjusted_expectancy_r),
        "maximum_symbol_concentration": round(maximum_symbol_concentration, 6),
        "reasons": reasons,
        "profile": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
    }


def resolve_regime_ensemble_candidate(
    primary_resolution: Mapping[str, Any] | None,
    reversal_candidate: Mapping[str, Any] | None,
    *,
    multi_timeframe_context: Mapping[str, Any] | None = None,
    cost_context: Mapping[str, Any] | None = None,
    promotion_status: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Route by cost-adjusted EV while keeping an unvalidated challenger shadow-only."""

    cfg = normalize_small_account_regime_config(config)
    primary = dict(primary_resolution or {})
    reversal = dict(reversal_candidate or {})
    promotion = dict(promotion_status or {})
    reversal_live = bool(
        reversal.get("allowed")
        and reversal.get("side") in {"long", "short"}
        and (
            not cfg["promotion_required"]
            or promotion.get("live_allowed")
        )
    )
    primary_engine = (
        "change_point_flow"
        if str(primary.get("source") or "") in {"event_only", "event_conflict_winner"}
        else "trend_continuation"
    )
    primary_side_sign = 1.0 if str(primary.get("side") or "").lower() == "long" else -1.0
    mtf_score = float(
        _finite((multi_timeframe_context or {}).get("weighted_direction_score"), 0.0)
        or 0.0
    )
    if (
        primary.get("allowed")
        and primary_engine == "trend_continuation"
        and bool((multi_timeframe_context or {}).get("available"))
        and bool((multi_timeframe_context or {}).get("ambiguous"))
    ):
        primary = {
            **primary,
            "allowed": False,
            "code": "REGIME_ROUTER_TIMEFRAME_AMBIGUOUS",
            "reason": "multi-timeframe trend state is ambiguous",
        }
    if (
        primary.get("allowed")
        and primary_engine == "trend_continuation"
        and primary_side_sign * mtf_score
        <= -float(cfg["multi_timeframe_strong_direction_score"])
    ):
        primary = {
            **primary,
            "allowed": False,
            "code": "REGIME_ROUTER_HIGHER_TIMEFRAME_CONFLICT",
            "reason": f"weighted higher-timeframe direction opposes entry ({mtf_score:+.3f})",
        }
    if (
        primary.get("allowed")
        and primary_engine == "trend_continuation"
        and bool((multi_timeframe_context or {}).get("mature"))
        and not bool(primary.get("fresh_continuation"))
    ):
        primary = {
            **primary,
            "allowed": False,
            "code": "REGIME_ROUTER_MATURE_TREND_WAIT_PULLBACK",
            "reason": "mature multi-timeframe trend requires fresh pullback/breakout resumption",
        }
    primary_ev = (
        _candidate_net_ev(
            primary,
            engine=primary_engine,
            multi_timeframe_context=multi_timeframe_context,
            cost_context=cost_context,
            config=cfg,
        )
        if primary.get("allowed") and primary.get("side") in {"long", "short"}
        else None
    )
    reversal_ev = (
        _candidate_net_ev(
            reversal,
            engine="exhaustion_reversal",
            multi_timeframe_context=multi_timeframe_context,
            cost_context=cost_context,
            config=cfg,
        )
        if reversal.get("allowed") and reversal.get("side") in {"long", "short"}
        else None
    )
    metadata = {
        "regime_profile": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
        "reversal_candidate": {
            key: reversal.get(key) for key in ("allowed", "side", "score", "code", "reason")
        },
        "reversal_shadow_only": bool(reversal.get("allowed") and not reversal_live),
        "challenger_promotion": promotion,
        "primary_net_edge": primary_ev,
        "reversal_net_edge": reversal_ev,
        "multi_timeframe_context": dict(multi_timeframe_context or {}),
    }
    if primary_ev and primary_ev["net_ev_r"] < float(cfg["router_minimum_net_ev_r"]):
        primary = {
            **primary,
            "allowed": False,
            "code": "REGIME_ROUTER_PRIMARY_NET_EV_LOW",
            "reason": f"primary net EV {primary_ev['net_ev_r']:.3f}R below floor",
        }
    candidates: list[tuple[str, dict[str, Any], dict[str, float]]] = []
    if primary.get("allowed") and primary_ev:
        candidates.append((primary_engine, primary, primary_ev))
    if reversal_live and reversal_ev and reversal_ev["net_ev_r"] >= float(cfg["router_minimum_net_ev_r"]):
        candidates.append(("exhaustion_reversal", reversal, reversal_ev))
    if len(candidates) == 2:
        first, second = candidates
        gap = abs(first[2]["net_ev_r"] - second[2]["net_ev_r"])
        if first[1].get("side") != second[1].get("side") and gap < float(
            cfg["router_conflict_net_ev_gap_r"]
        ):
            return {
                **metadata,
                "allowed": False,
                "side": None,
                "score": 0.0,
                "source": "regime_router",
                "agreement": "ambiguous",
                "code": "REGIME_ROUTER_AMBIGUOUS_CONFLICT",
                "reason": f"opposite engines separated by only {gap:.3f}R net EV",
                "regime_engine": "none",
                "trend_score": float(primary.get("trend_score", 0.0) or 0.0),
                "event_score": float(primary.get("event_score", 0.0) or 0.0),
                "reversal_score": float(reversal.get("score", 0.0) or 0.0),
            }
    if candidates:
        engine, selected, selected_ev = max(candidates, key=lambda item: item[2]["net_ev_r"])
        if engine != "exhaustion_reversal":
            selected = dict(selected)
            selected.setdefault("regime_engine", engine)
            return {**selected, **metadata, "selected_net_edge": selected_ev}
        return {
            **metadata,
            "allowed": True,
            "side": reversal.get("side"),
            "score": float(reversal.get("score", 0.0) or 0.0),
            "source": "exhaustion_reversal",
            "agreement": "reversal_only",
            "reason": reversal.get("reason"),
            "code": reversal.get("code"),
            "regime_engine": "exhaustion_reversal",
            "trend_score": float(primary.get("trend_score", 0.0) or 0.0),
            "event_score": float(primary.get("event_score", 0.0) or 0.0),
            "reversal_score": float(reversal.get("score", 0.0) or 0.0),
            "selected_net_edge": selected_ev,
        }
    fallback = dict(primary)
    fallback.setdefault("regime_engine", "none")
    if reversal.get("allowed") and not reversal_live:
        fallback.update({
            "allowed": False,
            "code": "REGIME_CHALLENGER_SHADOW_ONLY",
            "reason": (
                "exhaustion reversal recorded in shadow; promotion gate "
                f"{int(promotion.get('sample_count', 0) or 0)}/{int(cfg['promotion_min_trades'])}"
            ),
        })
    return {**fallback, **metadata}


def reversal_exit_plan_overrides(
    source: Any,
    reversal_candidate: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return plan-only exit overrides; existing/open trend positions get none."""

    if str(source or "").strip().lower() != "exhaustion_reversal":
        return {}
    cfg = normalize_small_account_regime_config(config)
    candidate = dict(reversal_candidate or {})
    tp1_r = _bounded(
        candidate.get("reversal_tp1_r"), cfg["tp1_r_floor"], cfg["tp1_r_cap"], cfg["tp1_r_floor"]
    )
    tp2_r = _bounded(
        candidate.get("reversal_tp2_r"), cfg["tp2_r_floor"], cfg["tp2_r_cap"], cfg["tp2_r_floor"]
    )
    return {
        "adaptive_regime_engine": "exhaustion_reversal",
        "adaptive_regime_profile": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
        "partial_take_profit_enabled": True,
        "partial_take_profit_r_multiple": tp1_r,
        "partial_take_profit_ratio": cfg["tp1_ratio"],
        "second_take_profit_enabled": True,
        "second_take_profit_r_multiple": tp2_r,
        "second_take_profit_ratio": cfg["tp2_ratio"],
        "runner_pct": 0.0,
        "preserve_runner_qty": False,
        "atr_trailing_enabled": False,
        "runner_exit_enabled": False,
        "runner_chandelier_enabled": False,
        "adaptive_trend_pyramid_enabled": False,
        "convex_rotation_exit_enabled": False,
        "tp1_breakeven_enabled": True,
        "tp1_breakeven_trigger_r": tp1_r,
        "take_profit_front_run_atr": 0.0,
        "take_profit_front_run_pct": 0.0,
        "soft_stop_enabled": False,
        "ev_time_stop_enabled": True,
        "ev_time_stop_bars": int(cfg["time_stop_bars"]),
        "ev_time_stop_min_mfe_r": 0.20,
        "ev_time_stop_max_current_r": 0.10,
        "reversal_mean_target_price": candidate.get("reversal_mean_target_price"),
    }


__all__ = (
    "SMALL_ACCOUNT_REGIME_PROFILE_VERSION",
    "default_small_account_regime_config",
    "evaluate_multi_timeframe_regime",
    "evaluate_regime_challenger_promotion",
    "evaluate_small_account_exhaustion_reversal",
    "normalize_small_account_regime_config",
    "resolve_regime_ensemble_candidate",
    "resolve_small_account_evidence_allocation",
    "reversal_exit_plan_overrides",
)

"""Regime-routed challenger for the aggressive sub-$1,000 trend profile.

The existing adaptive trend and change-point engines remain the primary entry
paths.  This module adds one deliberately narrow alternative: a confirmed
liquidity sweep/reclaim while the broad 1h trend is weak or already agrees
with the reversal direction.  It never overrides a valid trend/event entry.

All helpers are pure.  They evaluate completed candles and return plan
metadata; they never place, cancel, or modify an exchange order.
"""

from __future__ import annotations

from math import isfinite
from statistics import median
from typing import Any, Mapping, Sequence


SMALL_ACCOUNT_REGIME_PROFILE_VERSION = "small_account_regime_ensemble_v1"


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
    for key in ("enabled", "crypto_only"):
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
    ratio_sum = normalized["tp1_ratio"] + normalized["tp2_ratio"]
    normalized["tp1_ratio"] /= ratio_sum
    normalized["tp2_ratio"] = 1.0 - normalized["tp1_ratio"]
    normalized["risk_tier"] = "base"
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


def evaluate_small_account_exhaustion_reversal(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    trend_metrics: Mapping[str, Any] | None = None,
    futures_context: Mapping[str, Any] | None = None,
    market_regime_context: Mapping[str, Any] | None = None,
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
        recent = candles[-volume_lookback:]
        volume_sum = sum(row["volume"] for row in recent)
        vwap = (
            sum(((row["high"] + row["low"] + row["close"]) / 3.0) * row["volume"] for row in recent)
            / max(volume_sum, 1e-9)
            if volume_sum > 0
            else latest["close"]
        )
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


def resolve_regime_ensemble_candidate(
    primary_resolution: Mapping[str, Any] | None,
    reversal_candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep a valid trend/event candidate; otherwise admit a confirmed reversal."""

    primary = dict(primary_resolution or {})
    reversal = dict(reversal_candidate or {})
    if primary.get("allowed"):
        primary.setdefault("regime_engine", "trend_continuation")
        primary["reversal_candidate"] = {
            key: reversal.get(key) for key in ("allowed", "side", "score", "code", "reason")
        }
        return primary
    if reversal.get("allowed") and reversal.get("side") in {"long", "short"}:
        return {
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
            "reversal_candidate": reversal,
        }
    primary.setdefault("regime_engine", "none")
    primary["reversal_candidate"] = {
        key: reversal.get(key) for key in ("allowed", "side", "score", "code", "reason")
    }
    return primary


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
    "evaluate_small_account_exhaustion_reversal",
    "normalize_small_account_regime_config",
    "resolve_regime_ensemble_candidate",
    "reversal_exit_plan_overrides",
)

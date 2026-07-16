"""Liquidation-exhaustion reversal strategy.

LXR waits for a completed, high-volume price shock accompanied by a material
drop in futures open interest.  It never enters on the shock itself: a later
completed candle must reclaim structure in the opposite direction and the live
order book must be healthy and non-conflicting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from statistics import median
from typing import Any, Mapping, Sequence


LXR_STRATEGY = "liquidation_exhaustion_reversal_v1"


def default_liquidation_exhaustion_reversal_config() -> dict[str, Any]:
    """Return deliberately broad, volatility-normalized live defaults."""

    return {
        "enabled": True,
        "live_enabled": False,
        "timeframe": "15m",
        "atr_period": 14,
        "shock_lookback_bars": 3,
        "shock_max_age_bars": 4,
        "shock_min_atr": 2.20,
        "shock_volume_lookback_bars": 32,
        "shock_volume_ratio_min": 1.60,
        "oi_drop_1h_min_pct": 0.35,
        "oi_drop_z_max": -0.75,
        "reclaim_min_atr": 0.35,
        "reclaim_max_fraction": 0.78,
        "reversal_body_min_atr": 0.18,
        "taker_support_long_min": 1.02,
        "taker_support_short_max": 0.98,
        "score_min": 68.0,
        "risk_multiplier_floor": 0.25,
        "risk_multiplier_cap": 0.45,
        "structure_buffer_atr": 0.15,
        "stop_atr_multiplier": 1.10,
        "take_profit_r_multiple": 2.60,
        "time_stop_bars": 8,
    }


@dataclass(frozen=True)
class LiquidationExhaustionDecision:
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


def _row_value(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    return float(_finite((row or {}).get(key), default) or default)


def _clean_rows(rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for row in rows or []:
        parsed = {
            key: _finite((row or {}).get(key))
            for key in ("open", "high", "low", "close", "volume")
        }
        if any(parsed[key] is None for key in ("open", "high", "low", "close")):
            continue
        cleaned.append({
            "timestamp": (row or {}).get("timestamp"),
            "open": float(parsed["open"]),
            "high": float(parsed["high"]),
            "low": float(parsed["low"]),
            "close": float(parsed["close"]),
            "volume": max(0.0, float(parsed["volume"] or 0.0)),
        })
    return cleaned


def _atr(rows: Sequence[Mapping[str, Any]], period: int) -> float | None:
    if len(rows) < period + 1:
        return None
    ranges: list[float] = []
    for index in range(1, len(rows)):
        high = _row_value(rows[index], "high")
        low = _row_value(rows[index], "low")
        previous_close = _row_value(rows[index - 1], "close")
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    window = ranges[-period:]
    value = sum(window) / len(window) if window else 0.0
    return value if value > 0 and isfinite(value) else None


def _shock_candidate(
    rows: Sequence[Mapping[str, Any]],
    *,
    atr: float,
    lookback: int,
    max_age: int,
    volume_lookback: int,
) -> dict[str, Any] | None:
    """Return the strongest recent shock that precedes the confirmation bar."""

    latest_index = len(rows) - 1
    earliest_end = max(1, latest_index - max_age)
    best: dict[str, Any] | None = None
    for end_index in range(earliest_end, latest_index):
        for length in range(1, lookback + 1):
            base_index = end_index - length
            if base_index < 0:
                continue
            base_close = _row_value(rows[base_index], "close")
            shock_close = _row_value(rows[end_index], "close")
            move = shock_close - base_close
            magnitude_atr = abs(move) / atr
            if magnitude_atr <= 0:
                continue
            segment = rows[base_index + 1:end_index + 1]
            preceding = rows[max(0, base_index - volume_lookback):base_index]
            baseline_volumes = [
                _row_value(row, "volume") for row in preceding if _row_value(row, "volume") > 0
            ]
            baseline_volume = median(baseline_volumes) if baseline_volumes else 0.0
            shock_volume = max((_row_value(row, "volume") for row in segment), default=0.0)
            volume_ratio = shock_volume / baseline_volume if baseline_volume > 0 else 0.0
            candidate = {
                "direction": "down" if move < 0 else "up",
                "side": "long" if move < 0 else "short",
                "base_index": base_index,
                "shock_end_index": end_index,
                "shock_age_bars": latest_index - end_index,
                "base_close": base_close,
                "shock_close": shock_close,
                "shock_move": move,
                "shock_atr": magnitude_atr,
                "shock_high": max(_row_value(row, "high") for row in segment),
                "shock_low": min(_row_value(row, "low") for row in segment),
                "shock_volume_ratio": volume_ratio,
            }
            if best is None or candidate["shock_atr"] > best["shock_atr"]:
                best = candidate
    return best


def evaluate_liquidation_exhaustion_reversal(
    rows: Sequence[Mapping[str, Any]] | None,
    derivatives: Mapping[str, Any] | None,
    l2_gate: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> LiquidationExhaustionDecision:
    """Evaluate a completed-candle LXR entry without placing an order."""

    cfg = {**default_liquidation_exhaustion_reversal_config(), **dict(config or {})}
    candles = _clean_rows(rows)
    atr_period = max(2, int(cfg["atr_period"]))
    minimum_rows = max(
        atr_period + 2,
        int(cfg["shock_volume_lookback_bars"]) + int(cfg["shock_lookback_bars"]) + 2,
    )
    if len(candles) < minimum_rows:
        return LiquidationExhaustionDecision(reason="insufficient_completed_candles")

    atr_value = _atr(candles, atr_period)
    if atr_value is None:
        return LiquidationExhaustionDecision(reason="atr_unavailable")
    shock = _shock_candidate(
        candles,
        atr=atr_value,
        lookback=max(1, int(cfg["shock_lookback_bars"])),
        max_age=max(1, int(cfg["shock_max_age_bars"])),
        volume_lookback=max(8, int(cfg["shock_volume_lookback_bars"])),
    )
    if not shock or float(shock["shock_atr"]) < float(cfg["shock_min_atr"]):
        return LiquidationExhaustionDecision(
            reason="shock_not_extreme",
            metrics={"atr": atr_value, **dict(shock or {})},
        )
    side = str(shock["side"])
    metrics = {"atr": atr_value, **shock}
    if float(shock["shock_volume_ratio"]) < float(cfg["shock_volume_ratio_min"]):
        return LiquidationExhaustionDecision(
            side=side,
            reason="shock_volume_not_expanded",
            metrics=metrics,
        )

    context = dict(derivatives or {})
    oi_1h = _finite(context.get("open_interest_change_1h"))
    oi_latest = _finite(context.get("open_interest_delta_pct"))
    oi_z = _finite(context.get("open_interest_delta_z"))
    effective_oi = oi_1h if oi_1h is not None else oi_latest
    oi_drop_confirmed = (
        effective_oi is not None
        and effective_oi <= -abs(float(cfg["oi_drop_1h_min_pct"]))
    ) or (oi_z is not None and oi_z <= float(cfg["oi_drop_z_max"]))
    metrics.update({
        "open_interest_change_1h": oi_1h,
        "open_interest_delta_pct": oi_latest,
        "open_interest_delta_z": oi_z,
        "oi_drop_confirmed": oi_drop_confirmed,
    })
    if effective_oi is None and oi_z is None:
        return LiquidationExhaustionDecision(
            side=side,
            reason="open_interest_data_missing",
            metrics=metrics,
        )
    if not oi_drop_confirmed:
        return LiquidationExhaustionDecision(
            side=side,
            reason="open_interest_drop_missing",
            metrics=metrics,
        )

    latest = candles[-1]
    previous = candles[-2]
    latest_close = _row_value(latest, "close")
    latest_open = _row_value(latest, "open")
    shock_size = max(abs(float(shock["shock_move"])), atr_value)
    if side == "long":
        reclaim_distance = latest_close - float(shock["shock_low"])
        structure_reclaimed = latest_close > _row_value(previous, "high")
        reversal_body = latest_close - latest_open
        structure_stop = float(shock["shock_low"])
    else:
        reclaim_distance = float(shock["shock_high"]) - latest_close
        structure_reclaimed = latest_close < _row_value(previous, "low")
        reversal_body = latest_open - latest_close
        structure_stop = float(shock["shock_high"])
    reclaim_atr = reclaim_distance / atr_value
    reclaim_fraction = reclaim_distance / shock_size
    body_atr = reversal_body / atr_value
    metrics.update({
        "entry_price": latest_close,
        "reclaim_atr": reclaim_atr,
        "reclaim_fraction": reclaim_fraction,
        "structure_reclaimed": structure_reclaimed,
        "reversal_body_atr": body_atr,
        "structure_stop": structure_stop,
        "signal_candle_ts": latest.get("timestamp"),
    })
    if reclaim_atr < float(cfg["reclaim_min_atr"]):
        return LiquidationExhaustionDecision(side=side, reason="reversal_reclaim_too_weak", metrics=metrics)
    if reclaim_fraction > float(cfg["reclaim_max_fraction"]):
        return LiquidationExhaustionDecision(side=side, reason="reversal_entry_too_late", metrics=metrics)
    if not structure_reclaimed:
        return LiquidationExhaustionDecision(side=side, reason="reversal_structure_not_reclaimed", metrics=metrics)
    if body_atr < float(cfg["reversal_body_min_atr"]):
        return LiquidationExhaustionDecision(side=side, reason="reversal_body_too_weak", metrics=metrics)

    l2 = dict(l2_gate or {})
    metrics["l2_state"] = l2.get("state")
    metrics["l2_direction_support"] = l2.get("direction_support")
    if not bool(l2.get("allowed", False)):
        return LiquidationExhaustionDecision(side=side, reason="l2_stressed", metrics=metrics)
    if l2.get("direction_support") in {"long", "short"} and l2.get("direction_support") != side:
        return LiquidationExhaustionDecision(side=side, reason="l2_direction_conflict", metrics=metrics)

    taker_ratio = _finite(context.get("taker_buy_sell_ratio"))
    taker_support = bool(
        taker_ratio is not None
        and (
            taker_ratio >= float(cfg["taker_support_long_min"])
            if side == "long"
            else taker_ratio <= float(cfg["taker_support_short_max"])
        )
    )
    l2_support = l2.get("direction_support") == side
    metrics.update({
        "taker_buy_sell_ratio": taker_ratio,
        "taker_reversal_support": taker_support,
        "l2_reversal_support": l2_support,
    })

    shock_strength = min(1.0, float(shock["shock_atr"]) / max(float(cfg["shock_min_atr"]) * 1.75, 1e-9))
    volume_strength = min(1.0, float(shock["shock_volume_ratio"]) / max(float(cfg["shock_volume_ratio_min"]) * 1.75, 1e-9))
    oi_strength = min(1.0, max(0.0, -float(effective_oi or 0.0)) / max(float(cfg["oi_drop_1h_min_pct"]) * 2.5, 1e-9))
    score = 35.0 + 15.0 * shock_strength + 12.0 * volume_strength + 16.0 * oi_strength
    score += 10.0 if structure_reclaimed else 0.0
    score += 6.0 if taker_support else 0.0
    score += 6.0 if l2_support else 0.0
    score = min(100.0, score)
    metrics["score"] = score
    if score < float(cfg["score_min"]):
        return LiquidationExhaustionDecision(
            side=side,
            score=score,
            reason="score_below_threshold",
            metrics=metrics,
        )

    floor = max(0.0, float(cfg["risk_multiplier_floor"]))
    cap = max(floor, min(1.0, float(cfg["risk_multiplier_cap"])))
    quality = max(0.0, min(1.0, (score - float(cfg["score_min"])) / max(100.0 - float(cfg["score_min"]), 1e-9)))
    risk = floor + (cap - floor) * quality
    risk = min(risk, max(0.0, float(l2.get("risk_multiplier", 0.0) or 0.0)))
    return LiquidationExhaustionDecision(
        side=side,
        allowed=True,
        score=score,
        risk_multiplier=risk,
        reason=(
            f"LXR {side.upper()}: {float(shock['shock_atr']):.2f}ATR "
            f"{shock['direction']} shock, volume x{float(shock['shock_volume_ratio']):.2f}, "
            f"OI 1h {float(effective_oi or 0.0):+.2f}%, structure reclaimed"
        ),
        metrics=metrics,
    )

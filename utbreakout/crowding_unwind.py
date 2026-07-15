"""Funding/open-interest crowding unwind strategy.

This is a directional reversal strategy, not a spot-perpetual carry engine. It
looks for an overcrowded derivatives position whose price progression has
stalled, then requires absorption, an opposite structure break and supportive
L2 state before allowing a reduced-risk entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Mapping, Sequence

CROWDING_UNWIND_STRATEGY = "funding_oi_crowding_unwind_v1"


def default_crowding_unwind_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "live_enabled": False,
        "funding_percentile_min": 95.0,
        "funding_abs_min": 0.0008,
        "oi_z_min": 1.8,
        "oi_change_4h_min_pct": 4.0,
        "long_short_ratio_high": 1.65,
        "long_short_ratio_low": 0.61,
        "lookback_bars": 8,
        "structure_lookback_bars": 3,
        "progress_max_atr": 0.55,
        "taker_absorption_ratio": 1.08,
        "ofi_absorption_min": 10.0,
        "minimum_confirmations": 2,
        "score_min": 62.0,
        "risk_multiplier_floor": 0.30,
        "risk_multiplier_cap": 0.65,
        "stop_atr_multiplier": 1.35,
        "take_profit_r_multiple": 2.25,
        "time_stop_bars": 24,
        "l2_direction_required": True,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _value(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    return float(_finite((row or {}).get(key), default) or default)


def _true_range(rows: Sequence[Mapping[str, Any]]) -> float:
    if not rows:
        return 0.0
    values = []
    for index, row in enumerate(rows):
        high, low = _value(row, "high"), _value(row, "low")
        prev_close = _value(rows[index - 1], "close") if index else _value(row, "close")
        values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    window = values[-min(14, len(values)):]
    return sum(window) / len(window) if window else 0.0


@dataclass(frozen=True)
class CrowdingUnwindDecision:
    side: str | None = None
    allowed: bool = False
    score: float = 0.0
    risk_multiplier: float = 0.0
    reason: str = "not_evaluated"
    metrics: dict[str, Any] = field(default_factory=dict)


def _crowding_side(context: Mapping[str, Any], cfg: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    funding_raw = context.get("funding_rate")
    funding = _finite(funding_raw)
    percentile_raw = context.get("funding_percentile_30d", context.get("funding_percentile_7d"))
    percentile = _finite(percentile_raw)
    oi_z_raw = context.get("open_interest_delta_z")
    oi_z = _finite(oi_z_raw)
    oi_4h_raw = context.get("open_interest_change_4h")
    oi_4h = _finite(oi_4h_raw)
    long_short_raw = context.get("long_short_ratio")
    long_short = _finite(long_short_raw)

    missing_fields: list[str] = []
    if funding is None:
        missing_fields.append("funding_rate")
    if oi_z is None and oi_4h is None:
        missing_fields.append("open_interest_delta_z|open_interest_change_4h")
    if long_short is None:
        missing_fields.append("long_short_ratio")

    metrics = {
        "funding_rate": funding,
        "funding_percentile": percentile,
        "oi_z": oi_z,
        "oi_change_4h_pct": oi_4h,
        "long_short_ratio": long_short,
        "funding_rate_present": funding is not None,
        "funding_percentile_present": percentile is not None,
        "oi_z_present": oi_z is not None,
        "oi_change_4h_present": oi_4h is not None,
        "long_short_ratio_present": long_short is not None,
        "derivatives_data_ready": not missing_fields,
        "missing_derivatives_fields": missing_fields,
        "oi_extreme": False,
        "long_crowded": False,
        "short_crowded": False,
    }
    if missing_fields:
        return None, metrics

    oi_extreme = (
        (oi_z is not None and oi_z >= float(cfg["oi_z_min"]))
        or (oi_4h is not None and oi_4h >= float(cfg["oi_change_4h_min_pct"]))
    )
    percentile_value = float(percentile or 0.0)
    positive_funding = float(funding) >= float(cfg["funding_abs_min"]) or (
        float(funding) > 0 and percentile_value >= float(cfg["funding_percentile_min"])
    )
    negative_funding = float(funding) <= -float(cfg["funding_abs_min"]) or (
        float(funding) < 0 and percentile_value >= float(cfg["funding_percentile_min"])
    )
    long_crowded = oi_extreme and positive_funding and float(long_short) >= float(cfg["long_short_ratio_high"])
    short_crowded = oi_extreme and negative_funding and float(long_short) <= float(cfg["long_short_ratio_low"])
    side = "short" if long_crowded else "long" if short_crowded else None
    metrics.update({
        "oi_extreme": oi_extreme,
        "long_crowded": long_crowded,
        "short_crowded": short_crowded,
    })
    return side, metrics


def evaluate_crowding_unwind(
    rows: Sequence[Mapping[str, Any]] | None,
    derivatives: Mapping[str, Any] | None,
    l2_gate: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> CrowdingUnwindDecision:
    cfg = {**default_crowding_unwind_config(), **dict(config or {})}
    candles = list(rows or [])
    lookback = max(4, int(cfg["lookback_bars"]))
    structure_lookback = max(2, int(cfg["structure_lookback_bars"]))
    if len(candles) < lookback + structure_lookback + 1:
        return CrowdingUnwindDecision(reason="insufficient_candles")
    context = dict(derivatives or {})
    side, metrics = _crowding_side(context, cfg)
    metrics["l2_gate"] = dict(l2_gate or {})
    if not bool(metrics.get("derivatives_data_ready", False)):
        return CrowdingUnwindDecision(
            reason="crowding_derivatives_data_missing",
            metrics=metrics,
        )
    if side is None:
        return CrowdingUnwindDecision(reason="crowding_not_extreme", metrics=metrics)
    if not bool((l2_gate or {}).get("allowed", False)):
        return CrowdingUnwindDecision(side=side, reason="l2_stressed", metrics=metrics)

    recent = candles[-lookback:]
    latest = candles[-1]
    previous_structure = candles[-structure_lookback - 1:-1]
    atr = _true_range(candles[-max(20, lookback + structure_lookback):])
    if atr <= 0:
        return CrowdingUnwindDecision(side=side, reason="atr_unavailable", metrics=metrics)
    start_close = _value(recent[0], "close")
    end_close = _value(latest, "close")
    crowded_progress_atr = (
        (end_close - start_close) / atr if side == "short" else (start_close - end_close) / atr
    )
    progression_failed = crowded_progress_atr <= float(cfg["progress_max_atr"])
    if side == "short":
        structure_level = min(_value(row, "low") for row in previous_structure)
        structure_break = end_close < structure_level
    else:
        structure_level = max(_value(row, "high") for row in previous_structure)
        structure_break = end_close > structure_level

    taker_ratio = float(_finite(context.get("taker_buy_sell_ratio"), 1.0) or 1.0)
    rolling_ofi = float(_finite(context.get("rolling_ofi_score"), 0.0) or 0.0)
    if side == "short":
        absorption = taker_ratio >= float(cfg["taker_absorption_ratio"]) and progression_failed
        absorption = absorption or rolling_ofi >= float(cfg["ofi_absorption_min"]) and end_close <= start_close
        l2_support = str((l2_gate or {}).get("state")) == "ask_pressure" or str((l2_gate or {}).get("direction_support")) == "short"
    else:
        absorption = taker_ratio <= 1.0 / max(float(cfg["taker_absorption_ratio"]), 1e-9) and progression_failed
        absorption = absorption or rolling_ofi <= -float(cfg["ofi_absorption_min"]) and end_close >= start_close
        l2_support = str((l2_gate or {}).get("state")) == "bid_support" or str((l2_gate or {}).get("direction_support")) == "long"

    confirmations = int(progression_failed) + int(absorption) + int(structure_break) + int(l2_support)
    metrics.update({
        "side": side,
        "atr": atr,
        "crowded_progress_atr": crowded_progress_atr,
        "progression_failed": progression_failed,
        "structure_break": structure_break,
        "structure_level": structure_level,
        "absorption": absorption,
        "l2_support": l2_support,
        "taker_buy_sell_ratio": taker_ratio,
        "rolling_ofi_score": rolling_ofi,
        "confirmations": confirmations,
    })
    required = max(2, int(cfg["minimum_confirmations"]))
    if not structure_break:
        return CrowdingUnwindDecision(side=side, reason="structure_break_missing", metrics=metrics)
    if bool(cfg["l2_direction_required"]) and not l2_support:
        return CrowdingUnwindDecision(side=side, reason="l2_reversal_support_missing", metrics=metrics)
    if confirmations < required:
        return CrowdingUnwindDecision(side=side, reason="insufficient_reversal_confirmations", metrics=metrics)

    score = 35.0
    score += min(20.0, max(0.0, metrics["funding_percentile"] - 80.0))
    score += min(15.0, max(0.0, metrics["oi_z"]) * 5.0)
    score += 10.0 if progression_failed else 0.0
    score += 10.0 if absorption else 0.0
    score += 10.0 if l2_support else 0.0
    score = min(100.0, score)
    if score < float(cfg["score_min"]):
        return CrowdingUnwindDecision(side=side, score=score, reason="score_below_threshold", metrics=metrics)
    l2_multiplier = float((l2_gate or {}).get("risk_multiplier", 0.0) or 0.0)
    risk = min(
        float(cfg["risk_multiplier_cap"]),
        max(float(cfg["risk_multiplier_floor"]), score / 100.0),
        max(0.0, l2_multiplier),
    )
    return CrowdingUnwindDecision(
        side=side,
        allowed=True,
        score=score,
        risk_multiplier=risk,
        reason=(
            f"Crowding unwind {side} score={score:.1f} funding={metrics['funding_rate']:+.6f} "
            f"OI-z={metrics['oi_z']:+.2f} confirmations={confirmations} L2={l2_gate.get('state')}"
        ),
        metrics=metrics,
    )

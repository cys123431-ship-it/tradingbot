"""Trend continuation entry helper for UT Breakout.

This module adds a second entry path:

1. Fresh UTBot signal path:
   handled by existing emas.py logic.

2. Trend continuation path:
   when UTBot has no fresh signal but still has a valid bias_side,
   allow a smaller position if higher timeframe direction and 15m
   continuation setup are aligned.

No exchange access, no secrets, no order placement here.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping


@dataclass(frozen=True)
class ContinuationEntryDecision:
    enabled: bool
    accepted: bool
    side: str | None
    mode: str
    risk_multiplier: float
    setup: str
    reason: str
    blockers: list[str]
    positives: list[str]


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _truthy_side(value: Any) -> str | None:
    text = str(value or "").lower()
    return text if text in {"long", "short"} else None


def _volume_multiplier(volume_ratio: float | None) -> tuple[float, str]:
    if volume_ratio is None:
        return 0.80, "volume unknown"
    if volume_ratio < 0.35:
        return 0.0, f"volume extremely weak {volume_ratio:.2f}<0.35"
    if volume_ratio < 0.55:
        return 0.50, f"volume weak {volume_ratio:.2f}"
    if volume_ratio < 0.75:
        return 0.75, f"volume moderate {volume_ratio:.2f}"
    return 1.00, f"volume ok {volume_ratio:.2f}"


def _score_multiplier(score: float | None, *, hard_floor: float, reduce_floor: float, label: str) -> tuple[float, str]:
    if score is None:
        return 0.85, f"{label} unknown"
    if score < hard_floor:
        return 0.0, f"{label} too low {score:.1f}<{hard_floor:.1f}"
    if score < reduce_floor:
        return 0.55, f"{label} weak {score:.1f}"
    return 1.0, f"{label} ok {score:.1f}"


def _direction_allowed(direction_decision: Any, side: str) -> tuple[bool, float, str]:
    if direction_decision is None:
        return True, 0.85, "direction decision missing -> neutral reduced"
    if side == "long":
        allowed = bool(getattr(direction_decision, "long_allowed", False))
    else:
        allowed = bool(getattr(direction_decision, "short_allowed", False))
    multiplier = _f(getattr(direction_decision, "size_multiplier", 1.0), 1.0) or 1.0
    reason = str(getattr(direction_decision, "reason", "") or "direction checked")
    return allowed, max(0.0, min(1.0, multiplier)), reason


def evaluate_trend_continuation_entry(
    *,
    side: str | None,
    candidate_type: str | None,
    cfg: Mapping[str, Any] | None,
    values: Mapping[str, Any] | None,
    status: Mapping[str, Any] | None = None,
    direction_decision: Any = None,
    quality_score_v2: Mapping[str, Any] | None = None,
    trend_health: Mapping[str, Any] | None = None,
    strategy_quality: Mapping[str, Any] | None = None,
) -> ContinuationEntryDecision:
    """Evaluate trend continuation entry.

    This is intentionally stricter than fresh signal entry but less brittle
    than the old all-filters-must-pass design.
    """
    cfg = cfg or {}
    values = values or {}
    status = status or {}
    quality_score_v2 = quality_score_v2 or {}
    trend_health = trend_health or {}
    strategy_quality = strategy_quality or {}

    enabled = bool(cfg.get("trend_continuation_entry_enabled", True))
    side = _truthy_side(side)

    if not enabled:
        return ContinuationEntryDecision(False, False, side, "disabled", 0.0, "none", "disabled", [], [])

    if side not in {"long", "short"}:
        return ContinuationEntryDecision(True, False, side, "no_side", 0.0, "none", "no valid side", ["no side"], [])

    ctype = str(candidate_type or "").lower()
    if ctype == "fresh_signal":
        return ContinuationEntryDecision(
            True,
            True,
            side,
            "fresh_signal_passthrough",
            1.0,
            "fresh_signal",
            "fresh UTBot signal path",
            [],
            ["fresh signal"],
        )

    if ctype not in {"bias_state", "bias_continuation", "trend_continuation"}:
        return ContinuationEntryDecision(
            True,
            False,
            side,
            "not_bias_state",
            0.0,
            "none",
            f"candidate_type {ctype or 'none'} is not continuation eligible",
            [f"candidate_type {ctype or 'none'}"],
            [],
        )

    blockers: list[str] = []
    positives: list[str] = []

    direction_ok, direction_mult, direction_reason = _direction_allowed(direction_decision, side)
    if not direction_ok:
        blockers.append(f"direction blocked: {direction_reason}")
    else:
        positives.append(direction_reason)

    close_value = _f(values.get("entry_price"))
    open_value = _f(values.get("open"))
    ema50 = _f(values.get("ema50"))
    ema50_prev = _f(values.get("ema50_prev"))
    vwap = _f(values.get("vwap"))
    bb_mid = _f(values.get("bb_mid"))
    atr_pct = _f(values.get("atr_pct"))
    volume_ratio = _f(values.get("volume_ratio"))
    range_expansion = _f(values.get("range_expansion_ratio"))
    adx = _f(values.get("adx"))
    plus_di = _f(values.get("plus_di"))
    minus_di = _f(values.get("minus_di"))

    if close_value is None or atr_pct is None or atr_pct <= 0:
        blockers.append("price/ATR data missing")
        return ContinuationEntryDecision(
            True,
            False,
            side,
            "data_missing",
            0.0,
            "none",
            "; ".join(blockers),
            blockers,
            positives,
        )

    # EMA trend alignment on 15m.
    ema_aligned = False
    if ema50 is not None and ema50_prev is not None:
        if side == "long":
            ema_aligned = close_value >= ema50 and ema50 >= ema50_prev
        else:
            ema_aligned = close_value <= ema50 and ema50 <= ema50_prev
    if ema_aligned:
        positives.append("15m EMA aligned")
    else:
        blockers.append("15m EMA not aligned")

    # DMI/ADX is soft but useful.
    min_adx = float(cfg.get("trend_continuation_min_adx", 14.0) or 14.0)
    dmi_ok = True
    if adx is not None and plus_di is not None and minus_di is not None:
        dmi_ok = plus_di > minus_di if side == "long" else minus_di > plus_di
        if adx >= min_adx and dmi_ok:
            positives.append(f"ADX/DMI aligned {adx:.1f}")
        elif adx < min_adx:
            positives.append(f"ADX weak {adx:.1f}<{min_adx:.1f} -> reduced")
        else:
            blockers.append("DMI against continuation side")
    else:
        positives.append("ADX/DMI missing -> neutral reduced")

    max_extension = float(cfg.get("trend_continuation_max_extension_atr", 2.20) or 2.20)
    ref_candidates = [item for item in (ema50, vwap, bb_mid) if item is not None and item > 0]
    extension_atr = None
    if ref_candidates:
        extension_atr = min(
            abs(close_value - ref) / max(abs(close_value), 1e-9) * 100.0 / max(atr_pct, 1e-9)
            for ref in ref_candidates
        )
        if extension_atr > max_extension:
            blockers.append(f"extension {extension_atr:.2f}ATR>{max_extension:.2f}ATR")
        else:
            positives.append(f"extension {extension_atr:.2f}ATR")
    else:
        positives.append("extension reference missing -> neutral")

    # Setup: pullback, rebreak, or flow continuation.
    pullback_ok = False
    rebreak_ok = False
    flow_ok = False

    for ref in ref_candidates:
        dist_pct = abs(close_value - ref) / max(abs(close_value), 1e-9) * 100.0
        dist_atr = dist_pct / max(atr_pct, 1e-9)
        if side == "long":
            pullback_ok = pullback_ok or (close_value >= ref and dist_atr <= max_extension)
        else:
            pullback_ok = pullback_ok or (close_value <= ref and dist_atr <= max_extension)

    if side == "long":
        for key in ("donchian_high_prev", "keltner_upper", "bb_upper"):
            level = _f(values.get(key))
            rebreak_ok = rebreak_ok or (level is not None and close_value > level)
    else:
        for key in ("donchian_low_prev", "keltner_lower", "bb_lower"):
            level = _f(values.get(key))
            rebreak_ok = rebreak_ok or (level is not None and close_value < level)

    candle_ok = True
    if open_value is not None:
        candle_ok = close_value >= open_value if side == "long" else close_value <= open_value

    flow_ok = (
        volume_ratio is not None
        and volume_ratio >= float(cfg.get("trend_continuation_flow_min_volume_ratio", 0.45) or 0.45)
        and range_expansion is not None
        and range_expansion >= float(cfg.get("trend_continuation_min_range_expansion", 1.03) or 1.03)
        and candle_ok
    )

    setup_parts = []
    if pullback_ok:
        setup_parts.append("pullback")
    if rebreak_ok:
        setup_parts.append("rebreak")
    if flow_ok:
        setup_parts.append("flow")

    if not setup_parts:
        blockers.append("no pullback/rebreak/flow continuation setup")
        setup = "none"
    else:
        setup = "/".join(setup_parts)
        positives.append(f"setup {setup}")

    volume_mult, volume_reason = _volume_multiplier(volume_ratio)
    if volume_mult <= 0:
        blockers.append(volume_reason)
    else:
        positives.append(volume_reason)

    q2_mult, q2_reason = _score_multiplier(
        _f(quality_score_v2.get("score")),
        hard_floor=float(cfg.get("trend_continuation_quality_hard_floor", 20.0) or 20.0),
        reduce_floor=float(cfg.get("trend_continuation_quality_reduce_floor", 45.0) or 45.0),
        label="quality",
    )
    if q2_mult <= 0:
        blockers.append(q2_reason)
    else:
        positives.append(q2_reason)

    trend_mult, trend_reason = _score_multiplier(
        _f(trend_health.get("score")),
        hard_floor=float(cfg.get("trend_continuation_trend_hard_floor", 18.0) or 18.0),
        reduce_floor=float(cfg.get("trend_continuation_trend_reduce_floor", 42.0) or 42.0),
        label="trend",
    )
    if trend_mult <= 0:
        blockers.append(trend_reason)
    else:
        positives.append(trend_reason)

    strategy_mult, strategy_reason = _score_multiplier(
        _f(strategy_quality.get("score")),
        hard_floor=float(cfg.get("trend_continuation_strategy_hard_floor", 12.0) or 12.0),
        reduce_floor=float(cfg.get("trend_continuation_strategy_reduce_floor", 42.0) or 42.0),
        label="strategy",
    )
    if strategy_mult <= 0:
        blockers.append(strategy_reason)
    else:
        positives.append(strategy_reason)

    if blockers:
        return ContinuationEntryDecision(
            True,
            False,
            side,
            "trend_continuation",
            0.0,
            setup,
            "BLOCK: " + "; ".join(blockers[:6]),
            blockers,
            positives,
        )

    base_mult = float(cfg.get("trend_continuation_base_risk_multiplier", 0.60) or 0.60)
    setup_mult = 1.0 if rebreak_ok or flow_ok else 0.75
    if adx is not None and adx < min_adx:
        setup_mult *= 0.85

    risk_multiplier = max(
        float(cfg.get("trend_continuation_min_risk_multiplier", 0.25) or 0.25),
        min(1.0, base_mult * setup_mult * direction_mult * volume_mult * q2_mult * trend_mult * strategy_mult),
    )

    return ContinuationEntryDecision(
        True,
        True,
        side,
        "trend_continuation",
        round(risk_multiplier, 2),
        setup,
        "ACCEPT: " + "; ".join(positives[:8]),
        [],
        positives,
    )

"""Research overlay for Adaptive Breakout Trend.

This module is deliberately conservative.  It never creates a trade by itself
and never increases an accepted position size.  It only rejects or scales down
an already-approved Adaptive Breakout Trend candidate using three pieces of
evidence that are unusually robust in the literature:

1. multi-horizon trend/regime persistence,
2. volatility-managed exposure, and
3. perpetual-futures carry/crowding (funding and basis).

The overlay is pure and order-agnostic so it can be unit-tested and backtested
without exchange access.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping


ADAPTIVE_RESEARCH_OVERLAY_PROFILE = "adaptive_trend_v10_regime_carry_vol"


def default_adaptive_research_overlay_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "minimum_adjusted_score": 62.0,
        # Risk-managed momentum: target the same hourly volatility used by the
        # parent strategy, but unlike the legacy 0.90 floor allow meaningful
        # de-risking in high-volatility regimes.  Exposure can never exceed 1x.
        "target_hourly_volatility": 0.012,
        "volatility_targeting_power": 0.50,
        "volatility_scale_floor": 0.35,
        "volatility_scale_cap": 1.00,
        "volatility_shock_soft_ratio": 1.35,
        "volatility_shock_hard_ratio": 2.75,
        "volatility_shock_score_penalty": 6.0,
        # Momentum is most reliable when the underlying state persists.  Fresh
        # breakout/crossover events may pass with lower continuity, but mature
        # continuation entries must clear this floor.
        "minimum_regime_continuity": 0.58,
        "transition_fast_retention_floor": 0.25,
        "transition_score_penalty": 8.0,
        "regime_score_adjustment": 6.0,
        # Direction-signed carry.  Funding is decimal (0.0004 = 4 bps); basis
        # is percentage points (0.15 = 0.15%).
        "adverse_funding_soft": 0.0004,
        "adverse_funding_hard": 0.0012,
        "adverse_basis_soft_pct": 0.15,
        "adverse_basis_hard_pct": 0.40,
        "carry_score_penalty_max": 10.0,
        "carry_risk_multiplier_floor": 0.55,
        "crowded_extension_atr": 0.80,
        "favorable_carry_score_bonus_max": 1.5,
    }


def normalize_adaptive_research_overlay_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = default_adaptive_research_overlay_config()
    if isinstance(config, Mapping):
        cfg.update(config)

    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["minimum_adjusted_score"] = _bounded(
        cfg.get("minimum_adjusted_score"), 45.0, 90.0, 62.0
    )
    cfg["target_hourly_volatility"] = _bounded(
        cfg.get("target_hourly_volatility"), 0.001, 0.10, 0.012
    )
    cfg["volatility_targeting_power"] = _bounded(
        cfg.get("volatility_targeting_power"), 0.0, 1.0, 0.50
    )
    cfg["volatility_scale_floor"] = _bounded(
        cfg.get("volatility_scale_floor"), 0.10, 1.0, 0.35
    )
    cfg["volatility_scale_cap"] = _bounded(
        cfg.get("volatility_scale_cap"), cfg["volatility_scale_floor"], 1.0, 1.0
    )
    cfg["volatility_shock_soft_ratio"] = _bounded(
        cfg.get("volatility_shock_soft_ratio"), 1.0, 5.0, 1.35
    )
    cfg["volatility_shock_hard_ratio"] = _bounded(
        cfg.get("volatility_shock_hard_ratio"),
        cfg["volatility_shock_soft_ratio"] + 0.05,
        10.0,
        2.75,
    )
    cfg["volatility_shock_score_penalty"] = _bounded(
        cfg.get("volatility_shock_score_penalty"), 0.0, 20.0, 6.0
    )
    cfg["minimum_regime_continuity"] = _bounded(
        cfg.get("minimum_regime_continuity"), 0.25, 0.90, 0.58
    )
    cfg["transition_fast_retention_floor"] = _bounded(
        cfg.get("transition_fast_retention_floor"), 0.0, 0.75, 0.25
    )
    cfg["transition_score_penalty"] = _bounded(
        cfg.get("transition_score_penalty"), 0.0, 25.0, 8.0
    )
    cfg["regime_score_adjustment"] = _bounded(
        cfg.get("regime_score_adjustment"), 0.0, 15.0, 6.0
    )
    cfg["adverse_funding_soft"] = _bounded(
        cfg.get("adverse_funding_soft"), 0.0, 0.01, 0.0004
    )
    cfg["adverse_funding_hard"] = _bounded(
        cfg.get("adverse_funding_hard"),
        cfg["adverse_funding_soft"] + 1e-6,
        0.03,
        0.0012,
    )
    cfg["adverse_basis_soft_pct"] = _bounded(
        cfg.get("adverse_basis_soft_pct"), 0.0, 5.0, 0.15
    )
    cfg["adverse_basis_hard_pct"] = _bounded(
        cfg.get("adverse_basis_hard_pct"),
        cfg["adverse_basis_soft_pct"] + 0.01,
        20.0,
        0.40,
    )
    cfg["carry_score_penalty_max"] = _bounded(
        cfg.get("carry_score_penalty_max"), 0.0, 25.0, 10.0
    )
    cfg["carry_risk_multiplier_floor"] = _bounded(
        cfg.get("carry_risk_multiplier_floor"), 0.10, 1.0, 0.55
    )
    cfg["crowded_extension_atr"] = _bounded(
        cfg.get("crowded_extension_atr"), 0.10, 5.0, 0.80
    )
    cfg["favorable_carry_score_bonus_max"] = _bounded(
        cfg.get("favorable_carry_score_bonus_max"), 0.0, 5.0, 1.5
    )
    return cfg


def evaluate_adaptive_research_overlay(
    side: str | None,
    metrics: Mapping[str, Any] | None,
    futures_context: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    *,
    base_score: float | None = None,
) -> dict[str, Any]:
    """Grade an already-approved trend candidate.

    Return contract:
      * allowed: whether the parent signal may remain eligible
      * risk_multiplier: 0..1 only; never an up-sizing multiplier
      * adjusted_score: parent score after regime/carry/shock adjustments
      * diagnostics: transparent components for logging/backtests
    """

    cfg = normalize_adaptive_research_overlay_config(config)
    normalized_side = str(side or "").strip().lower()
    values = dict(metrics or {})
    context = dict(futures_context or {})

    result: dict[str, Any] = {
        "profile": ADAPTIVE_RESEARCH_OVERLAY_PROFILE,
        "allowed": True,
        "code": "ADAPTIVE_RESEARCH_OVERLAY_DISABLED",
        "reason": "research overlay disabled",
        "risk_multiplier": 1.0,
        "adjusted_score": _finite(base_score, _finite(values.get("score"), 0.0)),
        "regime_continuity": None,
        "volatility_scale": 1.0,
        "volatility_shock_ratio": None,
        "carry_severity": 0.0,
        "carry_risk_multiplier": 1.0,
        "adverse_funding": None,
        "adverse_basis_pct": None,
        "transition_risk": False,
        "fresh_shape": False,
    }
    if not cfg["enabled"]:
        return result
    if normalized_side not in {"long", "short"}:
        result.update(
            allowed=False,
            code="REJECTED_RESEARCH_OVERLAY_SIDE",
            reason="candidate side unavailable",
            risk_multiplier=0.0,
        )
        return result

    direction = 1.0 if normalized_side == "long" else -1.0
    fresh_shape = bool(
        values.get("ema_crossover")
        or values.get("compression_breakout")
        or values.get("pullback_resumption")
        or values.get("impulse_breakout")
        or values.get("change_point_flow_entry")
    )
    result["fresh_shape"] = fresh_shape

    horizon_scores = _horizon_scores(values.get("horizon_scores"))
    continuity = _regime_continuity(
        direction,
        horizon_scores,
        weighted_momentum=_finite(values.get("weighted_momentum"), 0.0),
        trend_clarity=_finite(values.get("trend_clarity"), None),
        trend_efficiency=_finite(values.get("trend_efficiency"), None),
    )
    result["regime_continuity"] = continuity

    fast_retention = _finite(values.get("fast_momentum_retention"), None)
    fast_directional = None
    slow_directional = None
    if horizon_scores:
        horizons = sorted(horizon_scores)
        fast_directional = direction * horizon_scores[horizons[0]]
        slow_directional = direction * horizon_scores[horizons[-1]]
    transition_risk = bool(
        (fast_retention is not None and fast_retention < cfg["transition_fast_retention_floor"])
        or (
            fast_directional is not None
            and slow_directional is not None
            and slow_directional > 0.10
            and fast_directional < -0.10
        )
    )
    result["transition_risk"] = transition_risk
    result["fast_directional_momentum"] = fast_directional
    result["slow_directional_momentum"] = slow_directional

    short_vol = _finite(values.get("short_volatility"), None)
    long_vol = _finite(values.get("long_volatility"), None)
    volatility_scale = 1.0
    shock_ratio = None
    shock_severity = 0.0
    if short_vol is not None and short_vol > 0 and long_vol is not None and long_vol > 0:
        forecast_vol = max(short_vol, 0.65 * short_vol + 0.35 * long_vol)
        raw_scale = (
            cfg["target_hourly_volatility"] / max(forecast_vol, 1e-12)
        ) ** cfg["volatility_targeting_power"]
        volatility_scale = _clamp(
            raw_scale,
            cfg["volatility_scale_floor"],
            cfg["volatility_scale_cap"],
        )
        shock_ratio = short_vol / max(long_vol, 1e-12)
        shock_severity = _ramp(
            shock_ratio,
            cfg["volatility_shock_soft_ratio"],
            cfg["volatility_shock_hard_ratio"],
        )
        volatility_scale = max(
            cfg["volatility_scale_floor"],
            volatility_scale * (1.0 - 0.50 * shock_severity),
        )
    result["volatility_scale"] = _clamp(volatility_scale, 0.0, 1.0)
    result["volatility_shock_ratio"] = shock_ratio
    result["volatility_shock_severity"] = shock_severity

    funding_rate = _first_finite(
        context,
        "funding_rate",
        "current_funding_rate",
        "last_funding_rate",
    )
    basis_pct = _first_finite(
        context,
        "basis_pct",
        "basis_percent",
        "perpetual_basis_pct",
    )
    adverse_funding = direction * funding_rate if funding_rate is not None else None
    adverse_basis = direction * basis_pct if basis_pct is not None else None
    result["adverse_funding"] = adverse_funding
    result["adverse_basis_pct"] = adverse_basis

    funding_severity = (
        _ramp(
            adverse_funding,
            cfg["adverse_funding_soft"],
            cfg["adverse_funding_hard"],
        )
        if adverse_funding is not None
        else 0.0
    )
    basis_severity = (
        _ramp(
            adverse_basis,
            cfg["adverse_basis_soft_pct"],
            cfg["adverse_basis_hard_pct"],
        )
        if adverse_basis is not None
        else 0.0
    )
    carry_severity = max(funding_severity, basis_severity)
    carry_risk_multiplier = 1.0 - (
        1.0 - cfg["carry_risk_multiplier_floor"]
    ) * carry_severity
    result["carry_severity"] = carry_severity
    result["carry_risk_multiplier"] = _clamp(carry_risk_multiplier, 0.0, 1.0)

    parent_score = _finite(base_score, _finite(values.get("score"), 0.0))
    regime_adjustment = 0.0
    if continuity is not None:
        regime_adjustment = (continuity - 0.50) * 2.0 * cfg["regime_score_adjustment"]
    transition_penalty = cfg["transition_score_penalty"] if transition_risk else 0.0
    shock_penalty = cfg["volatility_shock_score_penalty"] * shock_severity
    carry_penalty = cfg["carry_score_penalty_max"] * carry_severity

    favorable_carry = 0.0
    if adverse_funding is not None and adverse_funding < 0:
        favorable_carry = max(favorable_carry, min(1.0, abs(adverse_funding) / max(cfg["adverse_funding_hard"], 1e-12)))
    if adverse_basis is not None and adverse_basis < 0:
        favorable_carry = max(favorable_carry, min(1.0, abs(adverse_basis) / max(cfg["adverse_basis_hard_pct"], 1e-12)))
    favorable_bonus = cfg["favorable_carry_score_bonus_max"] * favorable_carry

    adjusted_score = _clamp(
        parent_score
        + regime_adjustment
        + favorable_bonus
        - transition_penalty
        - shock_penalty
        - carry_penalty,
        0.0,
        100.0,
    )
    result.update(
        adjusted_score=adjusted_score,
        regime_score_adjustment=regime_adjustment,
        transition_score_penalty=transition_penalty,
        volatility_score_penalty=shock_penalty,
        carry_score_penalty=carry_penalty,
        favorable_carry_score_bonus=favorable_bonus,
    )

    continuity_risk = 1.0
    if continuity is not None and continuity < 0.75:
        continuity_risk = _clamp(0.65 + 0.35 * continuity / 0.75, 0.65, 1.0)
    if transition_risk:
        continuity_risk = min(continuity_risk, 0.75)
    result["continuity_risk_multiplier"] = continuity_risk

    risk_multiplier = min(
        1.0,
        result["volatility_scale"]
        * result["carry_risk_multiplier"]
        * continuity_risk,
    )
    result["risk_multiplier"] = _clamp(risk_multiplier, 0.0, 1.0)

    fast_distance = abs(_finite(values.get("signed_fast_ema_distance_atr"), 0.0))
    extreme_crowding = bool(
        carry_severity >= 0.95
        and fast_distance >= cfg["crowded_extension_atr"]
        and not fresh_shape
    )
    if extreme_crowding:
        result.update(
            allowed=False,
            code="REJECTED_RESEARCH_OVERLAY_CROWDED_EXTENSION",
            reason=(
                f"crowded {normalized_side} continuation: carry={carry_severity:.2f}, "
                f"extension={fast_distance:.2f} ATR"
            ),
            risk_multiplier=0.0,
        )
        return result

    if (
        continuity is not None
        and continuity < cfg["minimum_regime_continuity"]
        and not fresh_shape
    ):
        result.update(
            allowed=False,
            code="REJECTED_RESEARCH_OVERLAY_REGIME_PERSISTENCE",
            reason=(
                f"mature trend continuity {continuity:.2f} below "
                f"{cfg['minimum_regime_continuity']:.2f}"
            ),
            risk_multiplier=0.0,
        )
        return result

    if (
        shock_ratio is not None
        and shock_ratio >= cfg["volatility_shock_hard_ratio"]
        and transition_risk
        and not fresh_shape
    ):
        result.update(
            allowed=False,
            code="REJECTED_RESEARCH_OVERLAY_VOLATILITY_TRANSITION",
            reason=(
                f"volatility shock {shock_ratio:.2f}x coincides with trend transition"
            ),
            risk_multiplier=0.0,
        )
        return result

    if adjusted_score < cfg["minimum_adjusted_score"]:
        result.update(
            allowed=False,
            code="REJECTED_RESEARCH_OVERLAY_SCORE",
            reason=(
                f"research-adjusted score {adjusted_score:.1f} below "
                f"{cfg['minimum_adjusted_score']:.1f}"
            ),
            risk_multiplier=0.0,
        )
        return result

    result.update(
        allowed=True,
        code="ADAPTIVE_RESEARCH_OVERLAY_ALLOWED",
        reason=(
            f"regime={continuity if continuity is not None else 'n/a'}, "
            f"vol_x={result['volatility_scale']:.2f}, "
            f"carry={carry_severity:.2f}, risk_x={result['risk_multiplier']:.2f}"
        ),
    )
    return result


def _regime_continuity(
    direction: float,
    horizon_scores: Mapping[int, float],
    *,
    weighted_momentum: float,
    trend_clarity: float | None,
    trend_efficiency: float | None,
) -> float | None:
    components: list[tuple[float, float]] = []
    if horizon_scores:
        directional = [direction * value for value in horizon_scores.values()]
        vote_fraction = sum(value > 0.10 for value in directional) / len(directional)
        positive_strength = sum(_clamp(value / 2.0, 0.0, 1.0) for value in directional) / len(directional)
        components.extend(((vote_fraction, 0.35), (positive_strength, 0.20)))
    if isfinite(weighted_momentum):
        components.append((_clamp(direction * weighted_momentum, 0.0, 1.0), 0.20))
    if trend_clarity is not None:
        components.append((_clamp(trend_clarity, 0.0, 1.0), 0.15))
    if trend_efficiency is not None:
        components.append((_clamp(trend_efficiency, 0.0, 1.0), 0.10))
    if not components:
        return None
    total_weight = sum(weight for _, weight in components)
    return _clamp(sum(value * weight for value, weight in components) / total_weight, 0.0, 1.0)


def _horizon_scores(value: Any) -> dict[int, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[int, float] = {}
    for key, item in value.items():
        try:
            horizon = int(key)
            score = float(item)
        except (TypeError, ValueError):
            continue
        if horizon > 0 and isfinite(score):
            result[horizon] = score
    return result


def _first_finite(source: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _finite(source.get(key), None)
        if parsed is not None:
            return parsed
    return None


def _ramp(value: float | None, soft: float, hard: float) -> float:
    if value is None or value <= soft:
        return 0.0
    if hard <= soft:
        return 1.0
    return _clamp((value - soft) / (hard - soft), 0.0, 1.0)


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _bounded(value: Any, lower: float, upper: float, default: float) -> float:
    parsed = _finite(value, default)
    return _clamp(float(parsed), lower, upper)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))

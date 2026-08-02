"""Selective risk expansion for unusually strong aggregate signals.

This overlay deliberately leaves entry gates unchanged.  It can only increase
the size of a plan that has already passed every strategy and execution safety
check, and only when at least two independent strategies agree on direction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Mapping


def default_opportunity_risk_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "minimum_confirmations": 2,
        "opportunity_score_min": 80.0,
        "high_conviction_score_min": 86.0,
        "elite_score_min": 90.0,
        "two_signal_multiplier": 1.12,
        "three_signal_multiplier": 1.25,
        "four_plus_signal_multiplier": 1.35,
        "max_multiplier": 1.35,
        "max_boosted_risk_percent": 1.0,
        "normal_atr_pct_min": 0.25,
        "normal_atr_pct_max": 3.50,
        "minimum_sane_stop_pct": 0.20,
        "opportunity_stop_pct_max": 1.75,
        "high_conviction_stop_pct_max": 1.50,
        "max_fresh_chase_atr": 0.30,
        "max_fresh_extension_atr": 1.50,
        "l2_multiplier_min": 0.90,
        "high_conviction_l2_multiplier_min": 0.95,
        "require_nonnegative_daily_pnl": True,
        "require_neutral_strategy_allocator": True,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _bounded(value: Any, lower: float, upper: float, default: float) -> float:
    parsed = _finite(value, default)
    return max(lower, min(upper, float(parsed)))


def _boolean(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def normalize_opportunity_risk_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = default_opportunity_risk_config()
    cfg = {**defaults, **dict(config or {})}
    for key in (
        "enabled",
        "require_nonnegative_daily_pnl",
        "require_neutral_strategy_allocator",
    ):
        cfg[key] = _boolean(cfg.get(key), bool(defaults[key]))
    cfg["minimum_confirmations"] = int(
        _bounded(cfg.get("minimum_confirmations"), 2, 5, defaults["minimum_confirmations"])
    )
    for key, lower, upper in (
        ("opportunity_score_min", 0.0, 100.0),
        ("high_conviction_score_min", 0.0, 100.0),
        ("elite_score_min", 0.0, 100.0),
        ("two_signal_multiplier", 1.0, 2.0),
        ("three_signal_multiplier", 1.0, 2.0),
        ("four_plus_signal_multiplier", 1.0, 2.0),
        ("max_multiplier", 1.0, 2.0),
        ("max_boosted_risk_percent", 0.05, 10.0),
        ("normal_atr_pct_min", 0.0, 10.0),
        ("normal_atr_pct_max", 0.1, 20.0),
        ("minimum_sane_stop_pct", 0.01, 5.0),
        ("opportunity_stop_pct_max", 0.1, 10.0),
        ("high_conviction_stop_pct_max", 0.1, 10.0),
        ("max_fresh_chase_atr", 0.0, 5.0),
        ("max_fresh_extension_atr", 0.0, 10.0),
        ("l2_multiplier_min", 0.0, 1.0),
        ("high_conviction_l2_multiplier_min", 0.0, 1.0),
    ):
        cfg[key] = _bounded(cfg.get(key), lower, upper, float(defaults[key]))
    cfg["high_conviction_score_min"] = max(
        cfg["opportunity_score_min"], cfg["high_conviction_score_min"]
    )
    cfg["elite_score_min"] = max(
        cfg["high_conviction_score_min"], cfg["elite_score_min"]
    )
    cfg["normal_atr_pct_max"] = max(
        cfg["normal_atr_pct_min"], cfg["normal_atr_pct_max"]
    )
    cfg["high_conviction_stop_pct_max"] = min(
        cfg["opportunity_stop_pct_max"], cfg["high_conviction_stop_pct_max"]
    )
    cfg["high_conviction_l2_multiplier_min"] = max(
        cfg["l2_multiplier_min"], cfg["high_conviction_l2_multiplier_min"]
    )
    cfg["two_signal_multiplier"] = min(
        cfg["max_multiplier"], cfg["two_signal_multiplier"]
    )
    cfg["three_signal_multiplier"] = min(
        cfg["max_multiplier"],
        max(cfg["two_signal_multiplier"], cfg["three_signal_multiplier"]),
    )
    cfg["four_plus_signal_multiplier"] = min(
        cfg["max_multiplier"],
        max(cfg["three_signal_multiplier"], cfg["four_plus_signal_multiplier"]),
    )
    return cfg


def _score_value(value: Any) -> float | None:
    score = _finite(value)
    if score is None:
        return None
    if 0.0 <= score <= 1.0:
        score *= 100.0
    return max(0.0, min(100.0, score))


def _quality_score(plan: Mapping[str, Any]) -> tuple[float, str]:
    for key in (
        "quad_alpha_score",
        "profit_alpha_score",
        "entry_edge_score",
        "strategy_quality_score",
        "quality_score_v2_score",
        "feature_score_value",
        "vmt_score",
        "crowding_score",
        "lxr_score",
    ):
        value = _score_value(plan.get(key))
        if value is not None:
            return value, key
    return 0.0, "unavailable"


def _metric(plan: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _finite(plan.get(key))
        if value is not None:
            return value
    for container_key in ("vmt_metrics", "crowding_metrics", "lxr_metrics", "rspt_logs"):
        container = plan.get(container_key)
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            value = _finite(container.get(key))
            if value is not None:
                return value
    return None


def _l2_health(plan: Mapping[str, Any]) -> tuple[str, float, bool]:
    gate = plan.get("l2_gate") if isinstance(plan.get("l2_gate"), Mapping) else {}
    state = str(plan.get("l2_state") or gate.get("state") or "unavailable").lower()
    multiplier = _finite(
        plan.get("l2_risk_multiplier"), _finite(gate.get("risk_multiplier"), 1.0)
    )
    multiplier = max(0.0, min(1.0, float(multiplier)))
    allowed = bool(gate.get("allowed", state not in {"stressed", "stressed_thin"}))
    side = str(plan.get("side") or "").lower()
    support = str(gate.get("direction_support") or "").lower()
    directional = not support or support == side
    healthy_state = state in {"calm", "deep_balanced", "bid_support", "ask_pressure"}
    return state, multiplier, bool(allowed and directional and healthy_state)


@dataclass(frozen=True)
class OpportunityRiskDecision:
    multiplier: float
    tier: str
    quality_score: float
    quality_source: str
    confirmation_count: int
    atr_pct: float | None
    stop_distance_pct: float | None
    l2_state: str
    l2_multiplier: float
    daily_pnl_usdt: float | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_opportunity_risk(
    plan: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    *,
    daily_pnl_usdt: float | None = None,
) -> OpportunityRiskDecision:
    plan = dict(plan or {})
    cfg = normalize_opportunity_risk_config(config)
    quality, quality_source = _quality_score(plan)
    confirmations = int(
        max(1, min(5, _finite(plan.get("quad_alpha_confirmation_count"), 1.0) or 1.0))
    )
    entry = _finite(plan.get("entry_price"))
    stop_pct = _metric(plan, "risk_distance_pct")
    if stop_pct is None and entry and entry > 0:
        distance = _metric(plan, "risk_distance")
        if distance is not None:
            stop_pct = abs(distance) / entry * 100.0
    atr_pct = _metric(plan, "atr_pct")
    if atr_pct is None and entry and entry > 0:
        atr = _metric(plan, "atr")
        if atr is not None:
            atr_pct = abs(atr) / entry * 100.0
    chase = _metric(plan, "entry_chase_atr", "rspt_entry_chase_atr")
    extension = _metric(
        plan,
        "ev_extension_atr",
        "bias_continuation_extension_atr",
        "extension_atr",
    )
    allocator = _finite(plan.get("strategy_allocator_multiplier"), 1.0)
    daily_pnl = _finite(daily_pnl_usdt, _finite(plan.get("daily_pnl")))
    l2_state, l2_multiplier, l2_healthy = _l2_health(plan)

    blockers: list[str] = []
    if not cfg["enabled"]:
        blockers.append("disabled")
    if not plan.get("quad_alpha_agreement_state"):
        blockers.append("not_aggregate")
    if confirmations < cfg["minimum_confirmations"]:
        blockers.append(f"confirmations<{cfg['minimum_confirmations']}")
    if quality < cfg["opportunity_score_min"]:
        blockers.append(f"quality<{cfg['opportunity_score_min']:.0f}")
    if atr_pct is None or not (
        cfg["normal_atr_pct_min"] <= atr_pct <= cfg["normal_atr_pct_max"]
    ):
        blockers.append("volatility_outside_normal_band")
    if stop_pct is None or not (
        cfg["minimum_sane_stop_pct"] <= stop_pct <= cfg["opportunity_stop_pct_max"]
    ):
        blockers.append("stop_distance_not_eligible")
    if chase is not None and chase > cfg["max_fresh_chase_atr"]:
        blockers.append("entry_not_fresh")
    if extension is not None and extension > cfg["max_fresh_extension_atr"]:
        blockers.append("extension_too_large")
    if not l2_healthy or l2_multiplier < cfg["l2_multiplier_min"]:
        blockers.append("l2_not_high_quality")
    if cfg["require_nonnegative_daily_pnl"] and (
        daily_pnl is None or daily_pnl < 0.0
    ):
        blockers.append("daily_pnl_negative_or_unavailable")
    if cfg["require_neutral_strategy_allocator"] and allocator < 0.999:
        blockers.append("performance_allocator_reducing")

    tier = "baseline"
    multiplier = 1.0
    if not blockers:
        if (
            confirmations >= 4
            and quality >= cfg["elite_score_min"]
            and stop_pct <= cfg["high_conviction_stop_pct_max"]
            and l2_multiplier >= cfg["high_conviction_l2_multiplier_min"]
        ):
            tier = "elite_alignment"
            multiplier = cfg["four_plus_signal_multiplier"]
        elif (
            confirmations >= 3
            and quality >= cfg["high_conviction_score_min"]
            and stop_pct <= cfg["high_conviction_stop_pct_max"]
            and l2_multiplier >= cfg["high_conviction_l2_multiplier_min"]
        ):
            tier = "high_conviction_alignment"
            multiplier = cfg["three_signal_multiplier"]
        else:
            tier = "confirmed_opportunity"
            multiplier = cfg["two_signal_multiplier"]

        current_risk_pct = _finite(plan.get("risk_per_trade_percent"))
        if current_risk_pct is not None and current_risk_pct > 0:
            multiplier = min(
                multiplier,
                max(1.0, cfg["max_boosted_risk_percent"] / current_risk_pct),
            )
        multiplier = max(1.0, min(cfg["max_multiplier"], multiplier))
        if multiplier <= 1.000001:
            tier = "risk_cap_baseline"
            blockers.append("max_boosted_risk_percent_reached")

    reason = (
        f"{tier}: quality={quality:.1f}({quality_source}) confirmations={confirmations} "
        f"ATR%={atr_pct if atr_pct is not None else 'n/a'} "
        f"stop%={stop_pct if stop_pct is not None else 'n/a'} "
        f"L2={l2_state}/{l2_multiplier:.2f} dailyPnL="
        f"{daily_pnl if daily_pnl is not None else 'n/a'}"
    )
    if blockers:
        reason += f" blockers={','.join(blockers)}"
    return OpportunityRiskDecision(
        multiplier=float(multiplier),
        tier=tier,
        quality_score=float(quality),
        quality_source=quality_source,
        confirmation_count=confirmations,
        atr_pct=atr_pct,
        stop_distance_pct=stop_pct,
        l2_state=l2_state,
        l2_multiplier=l2_multiplier,
        daily_pnl_usdt=daily_pnl,
        reason=reason,
    )


def apply_opportunity_risk_to_plan(
    plan: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    *,
    daily_pnl_usdt: float | None = None,
) -> dict[str, Any]:
    updated = dict(plan or {})
    if updated.get("opportunity_risk_applied"):
        return updated
    decision = select_opportunity_risk(
        updated,
        config,
        daily_pnl_usdt=daily_pnl_usdt,
    )
    multiplier = float(decision.multiplier)
    if multiplier > 1.0:
        for key in (
            "qty",
            "risk_usdt",
            "max_risk_per_trade_usdt",
            "planned_notional",
            "planned_margin",
            "expected_profit_usdt",
            "position_notional",
            "position_cap_original_notional",
            "position_cap_original_risk_usdt",
            "position_cap_max_notional",
        ):
            value = _finite(updated.get(key))
            if value is not None:
                updated[key] = value * multiplier
        risk_pct = _finite(updated.get("risk_per_trade_percent"))
        if risk_pct is not None:
            updated["risk_per_trade_percent"] = risk_pct * multiplier
    updated.update(
        {
            "opportunity_risk_applied": True,
            "opportunity_risk_multiplier": multiplier,
            "opportunity_risk_tier": decision.tier,
            "opportunity_risk_reason": decision.reason,
            "opportunity_risk_decision": decision.as_dict(),
        }
    )
    return updated

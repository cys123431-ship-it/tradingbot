"""Opportunity-aware leverage selection for accepted futures entry plans.

The selector changes margin leverage, not the stop-loss budget.  Position
quantity remains anchored to the strategy's existing risk plan and is only
restored when an earlier fixed-leverage margin cap had reduced that plan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Mapping


def default_dynamic_leverage_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "min_leverage": 2,
        "base_leverage": 4,
        "max_leverage": 10,
        "normal_atr_pct_min": 0.25,
        "normal_atr_pct_max": 3.50,
        "high_volatility_atr_pct": 4.50,
        "opportunity_stop_pct_max": 2.00,
        "high_conviction_stop_pct_max": 1.50,
        "minimum_sane_stop_pct": 0.20,
        "opportunity_score_min": 78.0,
        "high_conviction_score_min": 86.0,
        "strong_single_score_min": 90.0,
        "opportunity_confirmations_min": 2,
        "high_conviction_confirmations_min": 3,
        "high_quality_l2_multiplier_min": 0.85,
        "high_conviction_l2_multiplier_min": 0.95,
        "high_quality_risk_multiplier_min": 0.80,
        "high_conviction_risk_multiplier_min": 0.90,
        "max_fresh_chase_atr": 0.30,
        "max_fresh_extension_atr": 1.50,
        "opportunity_time_stop_bars": 16,
        "high_leverage_time_stop_bars": 8,
        "opportunity_monitor_timeframe": "15m",
        "opportunity_time_stop_min_mfe_r": 0.55,
        "high_leverage_time_stop_min_mfe_r": 0.70,
        "opportunity_trailing_activation_r": 1.25,
        "high_leverage_trailing_activation_r": 1.00,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _bounded(value: Any, lower: float, upper: float, default: float) -> float:
    number = _finite(value, default)
    return max(lower, min(upper, float(number)))


def normalize_dynamic_leverage_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = {**default_dynamic_leverage_config(), **dict(config or {})}
    enabled = cfg.get("enabled", True)
    cfg["enabled"] = (
        enabled
        if isinstance(enabled, bool)
        else str(enabled).strip().lower() in {"1", "true", "yes", "on", "enabled"}
    )
    cfg["min_leverage"] = int(_bounded(cfg.get("min_leverage"), 1, 10, 2))
    cfg["max_leverage"] = int(
        _bounded(cfg.get("max_leverage"), cfg["min_leverage"], 10, 10)
    )
    cfg["base_leverage"] = int(
        _bounded(cfg.get("base_leverage"), cfg["min_leverage"], cfg["max_leverage"], 4)
    )
    for key, lower, upper in (
        ("normal_atr_pct_min", 0.0, 10.0),
        ("normal_atr_pct_max", 0.1, 20.0),
        ("high_volatility_atr_pct", 0.1, 30.0),
        ("opportunity_stop_pct_max", 0.1, 10.0),
        ("high_conviction_stop_pct_max", 0.1, 10.0),
        ("minimum_sane_stop_pct", 0.01, 5.0),
        ("opportunity_score_min", 0.0, 100.0),
        ("high_conviction_score_min", 0.0, 100.0),
        ("strong_single_score_min", 0.0, 100.0),
        ("high_quality_l2_multiplier_min", 0.0, 1.0),
        ("high_conviction_l2_multiplier_min", 0.0, 1.0),
        ("high_quality_risk_multiplier_min", 0.0, 1.0),
        ("high_conviction_risk_multiplier_min", 0.0, 1.0),
        ("max_fresh_chase_atr", 0.0, 5.0),
        ("max_fresh_extension_atr", 0.0, 10.0),
        ("opportunity_time_stop_min_mfe_r", 0.0, 5.0),
        ("high_leverage_time_stop_min_mfe_r", 0.0, 5.0),
        ("opportunity_trailing_activation_r", 0.1, 10.0),
        ("high_leverage_trailing_activation_r", 0.1, 10.0),
    ):
        default = float(default_dynamic_leverage_config()[key])
        cfg[key] = _bounded(cfg.get(key), lower, upper, default)
    cfg["normal_atr_pct_max"] = max(
        cfg["normal_atr_pct_min"], cfg["normal_atr_pct_max"]
    )
    cfg["high_volatility_atr_pct"] = max(
        cfg["normal_atr_pct_max"], cfg["high_volatility_atr_pct"]
    )
    cfg["high_conviction_stop_pct_max"] = min(
        cfg["opportunity_stop_pct_max"], cfg["high_conviction_stop_pct_max"]
    )
    cfg["high_conviction_score_min"] = max(
        cfg["opportunity_score_min"], cfg["high_conviction_score_min"]
    )
    cfg["strong_single_score_min"] = max(
        cfg["high_conviction_score_min"], cfg["strong_single_score_min"]
    )
    for key in ("opportunity_confirmations_min", "high_conviction_confirmations_min"):
        cfg[key] = int(
            _bounded(cfg.get(key), 1, 5, default_dynamic_leverage_config()[key])
        )
    cfg["high_conviction_confirmations_min"] = max(
        cfg["opportunity_confirmations_min"], cfg["high_conviction_confirmations_min"]
    )
    for key in ("opportunity_time_stop_bars", "high_leverage_time_stop_bars"):
        cfg[key] = int(
            _bounded(cfg.get(key), 1, 96, default_dynamic_leverage_config()[key])
        )
    cfg["high_leverage_time_stop_bars"] = min(
        cfg["opportunity_time_stop_bars"], cfg["high_leverage_time_stop_bars"]
    )
    monitor_timeframe = str(cfg.get("opportunity_monitor_timeframe") or "15m").lower()
    if monitor_timeframe not in {"1m", "3m", "5m", "15m", "30m", "1h"}:
        monitor_timeframe = "15m"
    cfg["opportunity_monitor_timeframe"] = monitor_timeframe
    return cfg


def _score_value(value: Any) -> float | None:
    score = _finite(value)
    if score is None:
        return None
    if 0.0 <= score <= 1.0:
        score *= 100.0
    return max(0.0, min(100.0, score))


def _quality_score(plan: Mapping[str, Any]) -> tuple[float, str]:
    preferred = (
        "quad_alpha_score",
        "triple_alpha_score",
        "dual_alpha_score",
        "vmt_score",
        "crowding_score",
        "lxr_score",
        "strategy_quality_score",
        "quality_score_v2_score",
        "profit_alpha_score",
        "entry_edge_score",
        "feature_score_value",
        "selector_quality_score",
        "trend_health_score",
    )
    for key in preferred:
        score = _score_value(plan.get(key))
        if score is not None:
            return score, key
    return 72.0, "neutral_default"


def _confirmation_count(plan: Mapping[str, Any]) -> int:
    for key in (
        "quad_alpha_confirmation_count",
        "triple_alpha_confirmation_count",
        "dual_alpha_confirmation_count",
    ):
        value = _finite(plan.get(key))
        if value is not None:
            return max(1, min(5, int(value)))
    return 1


def _nested_metric(plan: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _finite(plan.get(key))
        if value is not None:
            return value
    for container_key in (
        "vmt_metrics",
        "crowding_metrics",
        "lxr_metrics",
        "rspt_logs",
    ):
        container = plan.get(container_key)
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            value = _finite(container.get(key))
            if value is not None:
                return value
    return None


def _risk_quality_multiplier(plan: Mapping[str, Any]) -> float:
    values: list[float] = []
    for key in (
        "strategy_allocator_multiplier",
        "market_quality_risk_multiplier",
        "strategy_quality_risk_multiplier",
        "quality_score_v2_risk_multiplier",
        "volatility_risk_multiplier",
        "meta_label_risk_multiplier",
        "trend_health_risk_multiplier",
        "selector_quality_risk_multiplier",
        "feature_score_risk_multiplier",
        "vmt_risk_multiplier",
        "crowding_risk_multiplier",
        "lxr_risk_multiplier",
        "rspt_risk_multiplier",
    ):
        value = _finite(plan.get(key))
        if value is not None:
            values.append(max(0.0, min(1.0, value)))
    return min(values) if values else 1.0


def _l2_quality(plan: Mapping[str, Any]) -> tuple[str, float, bool, bool]:
    gate = plan.get("l2_gate") if isinstance(plan.get("l2_gate"), Mapping) else {}
    state = str(plan.get("l2_state") or gate.get("state") or "unavailable").lower()
    multiplier = _finite(
        plan.get("l2_risk_multiplier"), _finite(gate.get("risk_multiplier"), 1.0)
    )
    multiplier = max(0.0, min(1.0, float(multiplier)))
    allowed = bool(gate.get("allowed", state not in {"stressed", "stressed_thin"}))
    side = str(plan.get("side") or "").lower()
    support = str(gate.get("direction_support") or "").lower()
    directional_ok = not support or support == side
    high_quality = (
        allowed
        and directional_ok
        and state in {"deep_balanced", "bid_support", "ask_pressure", "calm"}
    )
    observable = state not in {"", "disabled", "unavailable", "unknown", "none"}
    return state, multiplier, high_quality, observable


@dataclass(frozen=True)
class DynamicLeverageDecision:
    leverage: int
    tier: str
    opportunity_score: float
    quality_score: float
    quality_source: str
    confirmation_count: int
    atr_pct: float | None
    stop_distance_pct: float | None
    l2_state: str
    l2_multiplier: float
    risk_quality_multiplier: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_dynamic_leverage(
    plan: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> DynamicLeverageDecision:
    plan = dict(plan or {})
    cfg = normalize_dynamic_leverage_config(config)
    legacy = int(
        _bounded(plan.get("leverage"), 1, cfg["max_leverage"], cfg["base_leverage"])
    )
    quality, quality_source = _quality_score(plan)
    confirmations = _confirmation_count(plan)
    entry = _finite(plan.get("entry_price"))
    stop_pct = _nested_metric(plan, "risk_distance_pct")
    if stop_pct is None and entry and entry > 0:
        distance = _finite(plan.get("risk_distance"))
        if distance is not None:
            stop_pct = abs(distance) / entry * 100.0
    atr_pct = _nested_metric(plan, "atr_pct")
    if atr_pct is None and entry and entry > 0:
        atr = _nested_metric(plan, "atr")
        if atr is not None:
            atr_pct = abs(atr) / entry * 100.0
    chase = _nested_metric(plan, "entry_chase_atr", "rspt_entry_chase_atr")
    extension = _nested_metric(
        plan,
        "ev_extension_atr",
        "bias_continuation_extension_atr",
        "extension_atr",
    )
    risk_quality = _risk_quality_multiplier(plan)
    l2_state, l2_multiplier, l2_high_quality, l2_observable = _l2_quality(plan)

    if not cfg["enabled"]:
        return DynamicLeverageDecision(
            leverage=legacy,
            tier="fixed",
            opportunity_score=0.0,
            quality_score=quality,
            quality_source=quality_source,
            confirmation_count=confirmations,
            atr_pct=atr_pct,
            stop_distance_pct=stop_pct,
            l2_state=l2_state,
            l2_multiplier=l2_multiplier,
            risk_quality_multiplier=risk_quality,
            reason=f"dynamic disabled; configured {legacy}x",
        )
    if atr_pct is None and not l2_observable:
        return DynamicLeverageDecision(
            leverage=legacy,
            tier="data_fallback",
            opportunity_score=float(legacy),
            quality_score=quality,
            quality_source=quality_source,
            confirmation_count=confirmations,
            atr_pct=atr_pct,
            stop_distance_pct=stop_pct,
            l2_state=l2_state,
            l2_multiplier=l2_multiplier,
            risk_quality_multiplier=risk_quality,
            reason=f"data_fallback: ATR and observable L2 unavailable; preserve {legacy}x",
        )

    normal_vol = (
        atr_pct is not None
        and cfg["normal_atr_pct_min"] <= atr_pct <= cfg["normal_atr_pct_max"]
    )
    high_vol = atr_pct is not None and atr_pct >= cfg["high_volatility_atr_pct"]
    sane_stop = stop_pct is not None and stop_pct >= cfg["minimum_sane_stop_pct"]
    opportunity_stop = sane_stop and stop_pct <= cfg["opportunity_stop_pct_max"]
    high_stop = sane_stop and stop_pct <= cfg["high_conviction_stop_pct_max"]
    fresh = (chase is None or chase <= cfg["max_fresh_chase_atr"]) and (
        extension is None or extension <= cfg["max_fresh_extension_atr"]
    )
    l2_opportunity = (
        l2_high_quality and l2_multiplier >= cfg["high_quality_l2_multiplier_min"]
    )
    opportunity = all(
        (
            confirmations >= cfg["opportunity_confirmations_min"],
            quality >= cfg["opportunity_score_min"],
            normal_vol,
            opportunity_stop,
            fresh,
            l2_opportunity,
            risk_quality >= cfg["high_quality_risk_multiplier_min"],
        )
    )
    high_conviction = all(
        (
            confirmations >= cfg["high_conviction_confirmations_min"],
            quality >= cfg["high_conviction_score_min"],
            normal_vol,
            high_stop,
            fresh,
            l2_high_quality,
            l2_multiplier >= cfg["high_conviction_l2_multiplier_min"],
            risk_quality >= cfg["high_conviction_risk_multiplier_min"],
        )
    )
    strong_single = all(
        (
            confirmations == 1,
            quality >= cfg["strong_single_score_min"],
            normal_vol,
            high_stop,
            fresh,
            l2_opportunity,
            risk_quality >= cfg["high_conviction_risk_multiplier_min"],
        )
    )

    opportunity_score = float(cfg["base_leverage"])
    if quality >= cfg["high_conviction_score_min"]:
        opportunity_score += 2.0
    elif quality >= cfg["opportunity_score_min"]:
        opportunity_score += 1.0
    elif quality < 68.0:
        opportunity_score -= 1.0
    if normal_vol:
        opportunity_score += 1.0
    elif high_vol:
        opportunity_score -= 2.0
    if high_stop:
        opportunity_score += 1.0
    elif stop_pct is not None and stop_pct > cfg["opportunity_stop_pct_max"]:
        opportunity_score -= 1.0
    if l2_opportunity:
        opportunity_score += 1.0
    elif l2_multiplier < 0.75:
        opportunity_score -= 1.0
    if confirmations >= 2:
        opportunity_score += 1.0
    if confirmations >= 3:
        opportunity_score += 1.0
    if not fresh:
        opportunity_score -= 2.0
    if risk_quality < 0.75:
        opportunity_score -= 1.0

    leverage = int(round(opportunity_score))
    cap = 5
    tier = "standard"
    if high_conviction:
        cap = cfg["max_leverage"]
        tier = "high_conviction"
    elif opportunity:
        cap = min(cfg["max_leverage"], 8)
        tier = "opportunity"
    elif strong_single:
        cap = min(cfg["max_leverage"], 6)
        tier = "strong_single"
    elif confirmations >= 2 and quality >= cfg["opportunity_score_min"]:
        cap = min(cfg["max_leverage"], 6)
        tier = "confirmed_standard"

    if not l2_observable:
        cap = min(cap, 5)
    if high_vol or (
        stop_pct is not None and stop_pct > cfg["opportunity_stop_pct_max"] * 1.5
    ):
        cap = min(cap, 4)
        tier = "defensive_volatility"
    if risk_quality < 0.60 or (not l2_high_quality and l2_multiplier < 0.60):
        cap = min(cap, 3)
        tier = "defensive_quality"
    if l2_state in {"stressed", "stressed_thin"}:
        cap = cfg["min_leverage"]
        tier = "defensive_l2"

    leverage = max(cfg["min_leverage"], min(cfg["max_leverage"], cap, leverage))
    reason = (
        f"{tier}: quality={quality:.1f}({quality_source}) confirmations={confirmations} "
        f"ATR%={atr_pct if atr_pct is not None else 'n/a'} "
        f"stop%={stop_pct if stop_pct is not None else 'n/a'} "
        f"L2={l2_state}/{l2_multiplier:.2f} riskQuality={risk_quality:.2f}"
    )
    return DynamicLeverageDecision(
        leverage=leverage,
        tier=tier,
        opportunity_score=opportunity_score,
        quality_score=quality,
        quality_source=quality_source,
        confirmation_count=confirmations,
        atr_pct=atr_pct,
        stop_distance_pct=stop_pct,
        l2_state=l2_state,
        l2_multiplier=l2_multiplier,
        risk_quality_multiplier=risk_quality,
        reason=reason,
    )


def apply_dynamic_leverage_to_plan(
    plan: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    *,
    free_balance: float | None = None,
    safety_buffer: float = 0.98,
) -> dict[str, Any]:
    updated = dict(plan or {})
    cfg = normalize_dynamic_leverage_config(config)
    original_exit_fields = {
        "ev_time_stop_enabled": "dynamic_leverage_original_ev_time_stop_enabled",
        "ev_time_stop_bars": "dynamic_leverage_original_ev_time_stop_bars",
        "ev_time_stop_min_mfe_r": "dynamic_leverage_original_ev_time_stop_min_mfe_r",
        "atr_trailing_activation_r": "dynamic_leverage_original_atr_trailing_activation_r",
    }
    for field, original_field in original_exit_fields.items():
        if original_field not in updated and field in updated:
            updated[original_field] = updated.get(field)
        elif original_field in updated:
            updated[field] = updated.get(original_field)
    decision = select_dynamic_leverage(updated, cfg)
    leverage = int(decision.leverage)
    updated.update(
        {
            "leverage": leverage,
            "dynamic_leverage_applied": bool(cfg["enabled"]),
            "dynamic_leverage_tier": decision.tier,
            "dynamic_leverage_score": float(decision.opportunity_score),
            "dynamic_leverage_reason": decision.reason,
            "dynamic_leverage_decision": decision.as_dict(),
        }
    )

    if not cfg["enabled"]:
        current_notional = max(
            0.0, float(_finite(updated.get("planned_notional"), 0.0))
        )
        if current_notional > 0:
            updated["planned_margin"] = current_notional / max(float(leverage), 1.0)
        return updated

    entry_for_notional = max(0.0, float(_finite(updated.get("entry_price"), 0.0)))
    qty_for_notional = max(0.0, float(_finite(updated.get("qty"), 0.0)))
    current_notional = max(
        0.0,
        float(
            _finite(
                updated.get("planned_notional"), qty_for_notional * entry_for_notional
            )
        ),
    )
    desired_notional = max(
        current_notional,
        float(_finite(updated.get("position_cap_original_notional"), 0.0)),
    )
    free = _finite(free_balance)
    if free is not None:
        margin_cap_notional = (
            max(0.0, free) * leverage * max(0.0, min(1.0, safety_buffer))
        )
        target_notional = min(desired_notional, margin_cap_notional)
        entry = max(0.0, float(_finite(updated.get("entry_price"), 0.0)))
        risk_distance = max(0.0, float(_finite(updated.get("risk_distance"), 0.0)))
        rr = max(
            0.0,
            float(
                _finite(
                    updated.get("effective_rr_multiple"),
                    _finite(updated.get("rr_multiple"), 0.0),
                )
            ),
        )
        if entry > 0 and target_notional >= 0:
            qty = target_notional / entry
            updated.update(
                {
                    "qty": qty,
                    "planned_notional": target_notional,
                    "planned_margin": target_notional / max(float(leverage), 1.0),
                    "risk_usdt": qty * risk_distance,
                    "expected_profit_usdt": qty * risk_distance * rr,
                    "position_cap_applied": target_notional + 1e-9 < desired_notional,
                    "position_cap_reason": (
                        "dynamic_leverage_margin_cap"
                        if target_notional + 1e-9 < desired_notional
                        else "dynamic_leverage_restored_risk_plan"
                    ),
                    "position_cap_original_notional": desired_notional,
                    "position_cap_max_notional": margin_cap_notional,
                    "dynamic_leverage_restored_notional": max(
                        0.0, target_notional - current_notional
                    ),
                }
            )
    elif current_notional > 0:
        updated["planned_margin"] = current_notional / max(float(leverage), 1.0)

    if leverage >= 8:
        updated["dynamic_leverage_monitor_timeframe"] = cfg[
            "opportunity_monitor_timeframe"
        ]
        updated["ev_time_stop_enabled"] = True
        updated["ev_time_stop_bars"] = min(
            int(
                updated.get("ev_time_stop_bars", cfg["high_leverage_time_stop_bars"])
                or cfg["high_leverage_time_stop_bars"]
            ),
            cfg["high_leverage_time_stop_bars"],
        )
        updated["ev_time_stop_min_mfe_r"] = max(
            float(updated.get("ev_time_stop_min_mfe_r", 0.0) or 0.0),
            cfg["high_leverage_time_stop_min_mfe_r"],
        )
        updated["atr_trailing_activation_r"] = min(
            float(
                updated.get(
                    "atr_trailing_activation_r",
                    cfg["high_leverage_trailing_activation_r"],
                )
                or cfg["high_leverage_trailing_activation_r"]
            ),
            cfg["high_leverage_trailing_activation_r"],
        )
    elif leverage >= 6:
        updated["dynamic_leverage_monitor_timeframe"] = cfg[
            "opportunity_monitor_timeframe"
        ]
        updated["ev_time_stop_enabled"] = True
        updated["ev_time_stop_bars"] = min(
            int(
                updated.get("ev_time_stop_bars", cfg["opportunity_time_stop_bars"])
                or cfg["opportunity_time_stop_bars"]
            ),
            cfg["opportunity_time_stop_bars"],
        )
        updated["ev_time_stop_min_mfe_r"] = max(
            float(updated.get("ev_time_stop_min_mfe_r", 0.0) or 0.0),
            cfg["opportunity_time_stop_min_mfe_r"],
        )
        updated["atr_trailing_activation_r"] = min(
            float(
                updated.get(
                    "atr_trailing_activation_r",
                    cfg["opportunity_trailing_activation_r"],
                )
                or cfg["opportunity_trailing_activation_r"]
            ),
            cfg["opportunity_trailing_activation_r"],
        )
    else:
        updated.pop("dynamic_leverage_monitor_timeframe", None)
    return updated

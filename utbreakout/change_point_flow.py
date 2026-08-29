"""Robust event-regime and order-flow engine for small trend accounts.

The engine is deliberately lightweight: it looks for a recent distribution
shift in completed entry-timeframe candles, then grades order flow and
open-interest participation.  It can create an independent directional
candidate or reinforce an established higher-timeframe trend.  It does not
forecast price, bypass shared safety gates, or require all inputs to be present.
"""

from __future__ import annotations

from math import exp, isfinite, log, sqrt
from statistics import median
from time import time
from typing import Any, Mapping, Sequence


CHANGE_POINT_FLOW_PROFILE_VERSION = "change_point_flow_v3_freshness"


def default_change_point_flow_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "recent_bars": 4,
        "baseline_bars": 32,
        "structure_lookback_bars": 8,
        "minimum_price_regime_score": 58.0,
        "strong_price_regime_score": 70.0,
        "persistent_flow_score": 67.0,
        "opposing_flow_score": 28.0,
        "strong_total_score": 70.0,
        "elite_total_score": 82.0,
        "base_initial_margin_fraction": 0.65,
        "strong_initial_margin_fraction": 0.70,
        "elite_initial_margin_fraction": 0.75,
        "new_regime_stop_atr_multiplier": 1.35,
        "persistent_flow_stop_atr_multiplier": 1.50,
        "established_trend_stop_atr_multiplier": 1.75,
        "fallback_stop_atr_multiplier": 2.00,
        "tradfi_opening_range_enabled": True,
        "independent_candidate_minimum_score": 60.0,
        "candidate_conflict_margin": 12.0,
        # A fast event can lead a neutral trend, but it must not overrule an
        # already actionable trend in the opposite direction by default.
        "allow_event_conflict_override": False,
        "orderflow_max_age_seconds": 90.0,
    }


def normalize_change_point_flow_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = default_change_point_flow_config()
    normalized = dict(defaults)
    if isinstance(config, Mapping):
        normalized.update(config)
    normalized["enabled"] = bool(normalized.get("enabled", True))
    normalized["recent_bars"] = int(
        _bounded(normalized.get("recent_bars"), 3.0, 8.0, 4.0)
    )
    normalized["baseline_bars"] = int(
        _bounded(normalized.get("baseline_bars"), 16.0, 96.0, 32.0)
    )
    normalized["structure_lookback_bars"] = int(
        _bounded(normalized.get("structure_lookback_bars"), 4.0, 20.0, 8.0)
    )
    for key, lower, upper in (
        ("minimum_price_regime_score", 45.0, 80.0),
        ("strong_price_regime_score", 55.0, 90.0),
        ("persistent_flow_score", 55.0, 90.0),
        ("opposing_flow_score", 10.0, 45.0),
        ("strong_total_score", 60.0, 90.0),
        ("elite_total_score", 70.0, 98.0),
        ("base_initial_margin_fraction", 0.40, 0.80),
        ("strong_initial_margin_fraction", 0.45, 0.90),
        ("elite_initial_margin_fraction", 0.50, 0.95),
        ("new_regime_stop_atr_multiplier", 1.0, 2.5),
        ("persistent_flow_stop_atr_multiplier", 1.0, 2.5),
        ("established_trend_stop_atr_multiplier", 1.0, 3.0),
        ("fallback_stop_atr_multiplier", 1.25, 3.0),
        ("independent_candidate_minimum_score", 50.0, 85.0),
        ("candidate_conflict_margin", 5.0, 30.0),
        ("orderflow_max_age_seconds", 15.0, 300.0),
    ):
        normalized[key] = _bounded(
            normalized.get(key), lower, upper, float(defaults[key])
        )
    normalized["elite_total_score"] = max(
        normalized["strong_total_score"], normalized["elite_total_score"]
    )
    normalized["strong_initial_margin_fraction"] = max(
        normalized["base_initial_margin_fraction"],
        normalized["strong_initial_margin_fraction"],
    )
    normalized["elite_initial_margin_fraction"] = max(
        normalized["strong_initial_margin_fraction"],
        normalized["elite_initial_margin_fraction"],
    )
    normalized["tradfi_opening_range_enabled"] = bool(
        normalized.get("tradfi_opening_range_enabled", True)
    )
    event_override = normalized.get("allow_event_conflict_override", False)
    normalized["allow_event_conflict_override"] = (
        event_override
        if isinstance(event_override, bool)
        else str(event_override).strip().lower()
        in {"1", "true", "yes", "on", "enabled"}
    )
    return normalized


def evaluate_change_point_flow_entry(
    side: str | None,
    event_rows: Sequence[Mapping[str, Any]] | None,
    *,
    futures_context: Mapping[str, Any] | None = None,
    trend_metrics: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    tradfi: bool = False,
    allow_legacy_fallback: bool = True,
) -> dict[str, Any]:
    """Classify a small-account entry without creating an AND gate.

    Strong recent regime shifts, persistent directional flow, fresh pullback
    resumptions, and established high-quality trends are alternative paths.
    Missing derivatives data degrades to price/trend evidence; only a measured
    multi-source flow conflict is a hard veto for crypto entries.
    """

    cfg = normalize_change_point_flow_config(config)
    direction_name = str(side or "").strip().lower()
    direction = 1.0 if direction_name == "long" else -1.0
    metrics = dict(trend_metrics or {})
    context = dict(futures_context or {})
    result: dict[str, Any] = {
        "allowed": True,
        "code": "CHANGE_POINT_FLOW_DISABLED",
        "reason": "change-point flow overlay disabled",
        "profile": CHANGE_POINT_FLOW_PROFILE_VERSION,
        "state": "disabled",
        "risk_tier": "base",
        "initial_margin_fraction": cfg["base_initial_margin_fraction"],
        "stop_atr_multiplier": cfg["fallback_stop_atr_multiplier"],
        "override_soft_mature_veto": False,
        "event_structure_stop": None,
    }
    if not cfg["enabled"]:
        return result
    if direction_name not in {"long", "short"}:
        result.update({
            "allowed": False,
            "code": "REJECTED_CHANGE_POINT_FLOW_SIDE",
            "reason": "change-point flow side is unavailable",
            "state": "invalid",
        })
        return result

    rows = _clean_rows(event_rows)
    recent_bars = int(cfg["recent_bars"])
    baseline_bars = int(cfg["baseline_bars"])
    minimum_rows = baseline_bars + recent_bars + 1
    price_evidence_available = len(rows) >= minimum_rows
    price_score = 50.0
    regime_change_score = 0.0
    drift_z = 0.0
    breakout_atr = 0.0
    volume_ratio = 1.0
    volatility_expansion = 1.0
    persistence = 0.5
    atr = _finite(metrics.get("atr"), None)
    event_atr = None
    if price_evidence_available:
        window = rows[-minimum_rows:]
        closes = [row["close"] for row in window]
        returns = [
            log(closes[index] / closes[index - 1])
            for index in range(1, len(closes))
            if closes[index] > 0 and closes[index - 1] > 0
        ]
        baseline_returns = returns[:-recent_bars]
        recent_returns = returns[-recent_bars:]
        baseline_center = median(baseline_returns)
        baseline_scale = _robust_scale(baseline_returns, baseline_center)
        recent_mean = sum(recent_returns) / max(len(recent_returns), 1)
        drift_z = direction * (recent_mean - baseline_center) / max(
            baseline_scale / sqrt(max(len(recent_returns), 1)),
            1e-9,
        )
        prior = window[: -recent_bars]
        recent = window[-recent_bars:]
        prior_high = max(row["high"] for row in prior)
        prior_low = min(row["low"] for row in prior)
        latest_close = recent[-1]["close"]
        event_atr = _average_true_range(window, 14)
        if atr is None or atr <= 0:
            atr = event_atr
        breakout_atr = (
            (latest_close - prior_high) / max(event_atr, 1e-9)
            if direction > 0
            else (prior_low - latest_close) / max(event_atr, 1e-9)
        )
        baseline_volumes = [row["volume"] for row in prior if row["volume"] > 0]
        recent_volumes = [row["volume"] for row in recent if row["volume"] > 0]
        if baseline_volumes and recent_volumes:
            volume_ratio = (
                sum(recent_volumes) / len(recent_volumes)
            ) / max(median(baseline_volumes), 1e-9)
        baseline_vol = _rms(baseline_returns)
        recent_vol = _rms(recent_returns)
        volatility_expansion = recent_vol / max(baseline_vol, 1e-9)
        persistence = sum(
            1 for value in recent_returns if direction * value > 0
        ) / max(len(recent_returns), 1)

        drift_component = _score_from_signed(drift_z, scale=2.5)
        breakout_component = _score_from_signed(breakout_atr, scale=0.75)
        volume_component = _score_from_signed(volume_ratio - 1.0, scale=0.75)
        volatility_component = _score_from_signed(
            volatility_expansion - 1.0, scale=1.0
        )
        persistence_component = 100.0 * persistence
        price_score = _clamp(
            0.34 * drift_component
            + 0.25 * breakout_component
            + 0.16 * volume_component
            + 0.10 * volatility_component
            + 0.15 * persistence_component,
            0.0,
            100.0,
        )
        # A bounded, interpretable shift strength. This is BOCPD-inspired but
        # intentionally not presented as a calibrated probability.
        regime_change_score = 100.0 * _sigmoid(
            0.85 * max(0.0, drift_z - 0.5)
            + 0.75 * max(0.0, breakout_atr)
            + 0.35 * max(0.0, volume_ratio - 1.0)
            - 1.0
        )

        structure_rows = rows[-int(cfg["structure_lookback_bars"]):]
        result["event_structure_stop"] = (
            min(row["low"] for row in structure_rows)
            if direction > 0
            else max(row["high"] for row in structure_rows)
        )

    flow_score, flow_sources, flow_components, flow_freshness = (
        _directional_flow_score(
            direction,
            context,
            max_age_seconds=float(cfg["orderflow_max_age_seconds"]),
        )
    )
    oi_score, oi_sources = _open_interest_score(context)
    derivatives_sources = flow_sources + oi_sources

    opening_range_breakout = bool(
        tradfi
        and cfg["tradfi_opening_range_enabled"]
        and _tradfi_opening_range_breakout(rows, direction_name)
    )
    if opening_range_breakout:
        price_score = max(price_score, 76.0)
        regime_change_score = max(regime_change_score, 72.0)

    price_weight = 0.62 if tradfi else 0.52
    available_components = [(price_score, price_weight)]
    if flow_sources:
        available_components.append((flow_score, 0.20 if tradfi else 0.28))
    if oi_sources:
        available_components.append((oi_score, 0.08 if tradfi else 0.12))
    trend_score = _trend_quality_score(metrics, direction)
    available_components.append((trend_score, 0.18 if tradfi else 0.08))
    total_weight = sum(weight for _, weight in available_components)
    total_score = sum(
        score * weight for score, weight in available_components
    ) / max(total_weight, 1e-9)

    fresh_shape = bool(
        metrics.get("ema_crossover")
        or metrics.get("compression_breakout")
        or metrics.get("pullback_resumption")
        or metrics.get("impulse_breakout")
    )
    strong_trend = bool(
        abs(float(_finite(metrics.get("weighted_momentum"), 0.0) or 0.0)) >= 0.62
        and float(_finite(metrics.get("trend_clarity"), 0.0) or 0.0) >= 0.42
    )
    flow_nonopposing = not flow_sources or flow_score >= 42.0
    new_regime = bool(
        price_evidence_available
        and price_score >= cfg["strong_price_regime_score"]
        and regime_change_score >= 58.0
        and flow_nonopposing
    )
    persistent_flow = bool(
        flow_sources >= 2
        and flow_score >= cfg["persistent_flow_score"]
        and (price_score >= 48.0 or fresh_shape)
    )
    price_regime = bool(
        price_evidence_available
        and price_score >= cfg["minimum_price_regime_score"]
        and flow_nonopposing
    )
    established_trend = bool(strong_trend and flow_nonopposing)
    legacy_fallback = bool(
        allow_legacy_fallback
        and not price_evidence_available
        and not flow_sources
    )
    allowed_paths = {
        "new_regime": new_regime,
        "persistent_flow": persistent_flow,
        "fresh_trend_shape": fresh_shape and flow_nonopposing,
        "price_regime": price_regime,
        "established_trend": established_trend,
        "legacy_data_fallback": legacy_fallback,
        "tradfi_opening_range": opening_range_breakout,
    }

    strong_flow_conflict = bool(
        not tradfi
        and flow_sources >= 2
        and flow_score <= cfg["opposing_flow_score"]
        and price_score < 82.0
    )
    if strong_flow_conflict:
        allowed = False
        code = "REJECTED_CHANGE_POINT_FLOW_CONFLICT"
        state = "opposing_flow"
        reason = (
            f"measured order flow opposes {direction_name} "
            f"({flow_score:.1f}/100 across {flow_sources} sources)"
        )
    elif any(allowed_paths.values()):
        allowed = True
        if new_regime or opening_range_breakout:
            code = "CHANGE_POINT_FLOW_NEW_REGIME"
            state = "new_regime"
        elif persistent_flow:
            code = "CHANGE_POINT_FLOW_PERSISTENT"
            state = "persistent_flow"
        elif fresh_shape:
            code = "CHANGE_POINT_FLOW_FRESH_TREND_SHAPE"
            state = "fresh_trend_shape"
        elif price_regime:
            code = "CHANGE_POINT_FLOW_PRICE_REGIME"
            state = "price_regime"
        elif established_trend:
            code = "CHANGE_POINT_FLOW_ESTABLISHED_TREND"
            state = "established_trend"
        else:
            code = "CHANGE_POINT_FLOW_DATA_FALLBACK"
            state = "legacy_data_fallback"
        reason = (
            f"{state}: total={total_score:.1f}, price={price_score:.1f}, "
            f"flow={flow_score:.1f}, oi={oi_score:.1f}"
        )
    else:
        allowed = False
        code = "REJECTED_CHANGE_POINT_FLOW_NO_EDGE"
        state = "no_event_edge"
        reason = (
            f"no fresh regime/flow edge: total={total_score:.1f}, "
            f"price={price_score:.1f}, flow={flow_score:.1f}"
        )

    if total_score >= cfg["elite_total_score"] and (new_regime or persistent_flow):
        risk_tier = "elite"
    elif total_score >= cfg["strong_total_score"] and (
        new_regime or persistent_flow or price_regime or opening_range_breakout
    ):
        risk_tier = "strong"
    else:
        risk_tier = "base"
    margin_fraction = cfg[f"{risk_tier}_initial_margin_fraction"]
    if new_regime or opening_range_breakout:
        stop_multiplier = cfg["new_regime_stop_atr_multiplier"]
    elif persistent_flow or bool(metrics.get("pullback_resumption")):
        stop_multiplier = cfg["persistent_flow_stop_atr_multiplier"]
    elif established_trend or price_regime:
        stop_multiplier = cfg["established_trend_stop_atr_multiplier"]
    else:
        stop_multiplier = cfg["fallback_stop_atr_multiplier"]

    result.update({
        "allowed": allowed,
        "code": code,
        "reason": reason,
        "state": state,
        "risk_tier": risk_tier,
        "initial_margin_fraction": margin_fraction,
        "stop_atr_multiplier": stop_multiplier,
        "override_soft_mature_veto": bool(
            allowed and (new_regime or persistent_flow) and total_score >= 70.0
        ),
        "total_score": round(total_score, 2),
        "price_score": round(price_score, 2),
        "flow_score": round(flow_score, 2),
        "open_interest_score": round(oi_score, 2),
        "trend_score": round(trend_score, 2),
        "regime_change_score": round(regime_change_score, 2),
        "directional_drift_z": round(drift_z, 4),
        "breakout_atr": round(breakout_atr, 4),
        "volume_ratio": round(volume_ratio, 4),
        "volatility_expansion": round(volatility_expansion, 4),
        "directional_persistence": round(persistence, 4),
        "price_evidence_available": price_evidence_available,
        "flow_source_count": flow_sources,
        "open_interest_source_count": oi_sources,
        "derivatives_source_count": derivatives_sources,
        "flow_components": flow_components,
        "orderflow_snapshot_ts": flow_freshness["snapshot_ts"],
        "orderflow_age_seconds": flow_freshness["age_seconds"],
        "orderflow_stale": flow_freshness["stale"],
        "paths": allowed_paths,
        "tradfi": bool(tradfi),
        "event_atr": event_atr,
        "event_reference_price": rows[-1]["close"] if rows else None,
        "event_signal_candle_ts": rows[-1].get("timestamp") if rows else None,
    })
    return result


def select_independent_change_point_flow_candidate(
    event_rows: Sequence[Mapping[str, Any]] | None,
    *,
    futures_context: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    tradfi: bool = False,
) -> dict[str, Any]:
    """Create a direction candidate without requiring the 1h trend engine."""

    cfg = normalize_change_point_flow_config(config)
    evaluations = {
        side: evaluate_change_point_flow_entry(
            side,
            event_rows,
            futures_context=futures_context,
            trend_metrics={},
            config=cfg,
            tradfi=tradfi,
            allow_legacy_fallback=False,
        )
        for side in ("long", "short")
    }
    minimum_score = float(cfg["independent_candidate_minimum_score"])
    actionable = {
        side: result
        for side, result in evaluations.items()
        if bool(result.get("allowed"))
        and float(_finite(result.get("total_score"), 0.0) or 0.0)
        >= minimum_score
        and str(result.get("state") or "") != "legacy_data_fallback"
    }
    base = {
        "allowed": False,
        "side": None,
        "score": 0.0,
        "reason": "no independent change-point flow candidate",
        "code": "NO_INDEPENDENT_CHANGE_POINT_FLOW_CANDIDATE",
        "source": "change_point_flow",
        "evaluations": evaluations,
    }
    if not actionable:
        return base

    ranked = sorted(
        actionable.items(),
        key=lambda item: float(item[1].get("total_score", 0.0) or 0.0),
        reverse=True,
    )
    selected_side, selected = ranked[0]
    selected_score = float(selected.get("total_score", 0.0) or 0.0)
    if len(ranked) > 1:
        runner_up_score = float(ranked[1][1].get("total_score", 0.0) or 0.0)
        if selected_score - runner_up_score < float(cfg["candidate_conflict_margin"]):
            base.update({
                "code": "REJECTED_INDEPENDENT_DIRECTION_AMBIGUOUS",
                "reason": (
                    f"independent long/short scores are ambiguous: "
                    f"{selected_score:.1f} vs {runner_up_score:.1f}"
                ),
            })
            return base

    return {
        **base,
        "allowed": True,
        "side": selected_side,
        "score": selected_score,
        "reason": str(selected.get("reason") or "independent event candidate"),
        "code": str(selected.get("code") or "CHANGE_POINT_FLOW_CANDIDATE"),
        "decision": selected,
    }


def resolve_trend_event_candidate(
    trend_candidate: Mapping[str, Any] | None,
    event_candidate: Mapping[str, Any] | None,
    *,
    conflict_margin: float = 12.0,
    allow_event_conflict_override: bool = False,
) -> dict[str, Any]:
    """Combine independent trend and event candidates as weighted OR paths."""

    trend = dict(trend_candidate or {})
    event = dict(event_candidate or {})
    trend_allowed = bool(
        trend.get("allowed") and str(trend.get("side") or "") in {"long", "short"}
    )
    event_allowed = bool(
        event.get("allowed") and str(event.get("side") or "") in {"long", "short"}
    )
    trend_score = _clamp(float(_finite(trend.get("score"), 0.0) or 0.0), 0.0, 100.0)
    event_score = _clamp(float(_finite(event.get("score"), 0.0) or 0.0), 0.0, 100.0)
    waiting = {
        "allowed": False,
        "side": None,
        "source": "none",
        "agreement": "none",
        "score": 0.0,
        "reason": "neither trend nor event engine produced a candidate",
        "trend_score": trend_score,
        "event_score": event_score,
    }
    if not trend_allowed and not event_allowed:
        return waiting
    if trend_allowed and not event_allowed:
        return {
            **waiting,
            "allowed": True,
            "side": str(trend["side"]),
            "source": "trend_only",
            "agreement": "trend_only",
            "score": trend_score,
            "fresh_continuation": bool(trend.get("fresh_continuation")),
            "reason": "1h trend candidate; event engine neutral",
        }
    if event_allowed and not trend_allowed:
        return {
            **waiting,
            "allowed": True,
            "side": str(event["side"]),
            "source": "event_only",
            "agreement": "event_only",
            "score": event_score,
            "event_decision": dict(event.get("decision") or {}),
            "event_state": (event.get("decision") or {}).get("state"),
            "event_risk_tier": (event.get("decision") or {}).get("risk_tier"),
            "reason": "independent regime/order-flow candidate",
        }

    trend_side = str(trend["side"])
    event_side = str(event["side"])
    if trend_side == event_side:
        combined_score = _clamp(
            0.45 * trend_score + 0.55 * event_score + 8.0,
            0.0,
            100.0,
        )
        return {
            **waiting,
            "allowed": True,
            "side": trend_side,
            "source": "aligned",
            "agreement": "aligned",
            "score": combined_score,
            "fresh_continuation": bool(trend.get("fresh_continuation")),
            "event_decision": dict(event.get("decision") or {}),
            "event_state": (event.get("decision") or {}).get("state"),
            "event_risk_tier": (event.get("decision") or {}).get("risk_tier"),
            "reason": "1h trend and independent event engine aligned",
        }

    margin = _clamp(conflict_margin, 5.0, 30.0)
    score_gap = event_score - trend_score
    if score_gap >= margin and allow_event_conflict_override:
        return {
            **waiting,
            "allowed": True,
            "side": event_side,
            "source": "event_conflict_winner",
            "agreement": "conflict_resolved",
            "score": event_score,
            "event_decision": dict(event.get("decision") or {}),
            "event_state": (event.get("decision") or {}).get("state"),
            "event_risk_tier": (event.get("decision") or {}).get("risk_tier"),
            "reason": f"event candidate won conflict by {score_gap:.1f} points",
        }
    if score_gap >= margin:
        return {
            **waiting,
            "source": "conflict_wait",
            "agreement": "conflict",
            "reason": (
                f"event {event_side} leads by {score_gap:.1f} points but "
                f"cannot override actionable {trend_side} trend"
            ),
        }
    if score_gap <= -margin:
        return {
            **waiting,
            "allowed": True,
            "side": trend_side,
            "source": "trend_conflict_winner",
            "agreement": "conflict_resolved",
            "score": trend_score,
            "fresh_continuation": bool(trend.get("fresh_continuation")),
            "reason": f"trend candidate won conflict by {abs(score_gap):.1f} points",
        }
    return {
        **waiting,
        "source": "conflict_wait",
        "agreement": "conflict",
        "reason": (
            f"trend {trend_side} {trend_score:.1f} vs event "
            f"{event_side} {event_score:.1f}; gap below {margin:.1f}"
        ),
    }


def _clean_rows(
    rows: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, float]]:
    clean: list[dict[str, float]] = []
    for raw in rows or ():
        if not isinstance(raw, Mapping):
            continue
        values = {
            key: _finite(raw.get(key), None)
            for key in ("timestamp", "open", "high", "low", "close", "volume")
        }
        if any(values[key] is None for key in ("open", "high", "low", "close")):
            continue
        if values["close"] <= 0 or values["high"] < values["low"]:
            continue
        values["volume"] = max(0.0, float(values["volume"] or 0.0))
        clean.append(values)
    return clean


def _directional_flow_score(
    direction: float,
    context: Mapping[str, Any],
    *,
    max_age_seconds: float = 90.0,
) -> tuple[float, int, dict[str, float], dict[str, Any]]:
    snapshot_ts = _finite(context.get("orderflow_snapshot_ts"), None)
    if snapshot_ts is not None and snapshot_ts > 10_000_000_000:
        snapshot_ts /= 1000.0
    age_seconds = (
        max(0.0, time() - snapshot_ts)
        if snapshot_ts is not None and snapshot_ts > 0
        else None
    )
    stale = bool(
        age_seconds is not None
        and age_seconds > max(1.0, float(max_age_seconds))
    )
    freshness = {
        "snapshot_ts": snapshot_ts,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "stale": stale,
    }
    components: dict[str, float] = {}
    imbalance = (
        None
        if stale
        else _finite(context.get("rolling_orderbook_imbalance_pct"), None)
    )
    if imbalance is not None:
        components["orderbook_imbalance"] = _clamp(direction * imbalance / 18.0, -1.0, 1.0)
    imbalance_delta = (
        None
        if stale
        else _finite(context.get("rolling_orderbook_imbalance_delta"), None)
    )
    if imbalance_delta is not None:
        components["orderbook_delta"] = _clamp(
            direction * imbalance_delta / 10.0, -1.0, 1.0
        )
    taker_ratio = (
        None
        if stale
        else _finite(context.get("taker_buy_sell_ratio"), None)
    )
    if taker_ratio is not None and taker_ratio > 0:
        components["taker_ratio"] = _clamp(
            direction * log(taker_ratio) / log(1.18), -1.0, 1.0
        )
    if not components:
        return 50.0, 0, {}, freshness
    value = sum(components.values()) / len(components)
    return (
        50.0 + 50.0 * value,
        len(components),
        {key: round(component, 4) for key, component in components.items()},
        freshness,
    )


def _open_interest_score(context: Mapping[str, Any]) -> tuple[float, int]:
    components = []
    oi_z = _finite(context.get("open_interest_delta_z"), None)
    if oi_z is not None:
        components.append(_clamp(oi_z / 2.0, -1.0, 1.0))
    acceleration = _finite(context.get("open_interest_acceleration"), None)
    if acceleration is not None:
        components.append(_clamp(acceleration / 1.5, -1.0, 1.0))
    delta_4h = _finite(context.get("open_interest_change_4h"), None)
    if delta_4h is not None:
        components.append(_clamp(delta_4h / 5.0, -1.0, 1.0))
    if not components:
        return 50.0, 0
    return 50.0 + 50.0 * sum(components) / len(components), len(components)


def _trend_quality_score(metrics: Mapping[str, Any], direction: float) -> float:
    score = _finite(metrics.get("score"), None)
    if score is not None:
        return _clamp(score, 0.0, 100.0)
    momentum = direction * float(
        _finite(metrics.get("weighted_momentum"), 0.0) or 0.0
    )
    clarity = float(_finite(metrics.get("trend_clarity"), 0.0) or 0.0)
    efficiency = float(_finite(metrics.get("trend_efficiency"), 0.0) or 0.0)
    return _clamp(
        50.0 + 25.0 * momentum + 15.0 * clarity + 10.0 * efficiency,
        0.0,
        100.0,
    )


def _tradfi_opening_range_breakout(rows: Sequence[Mapping[str, float]], side: str) -> bool:
    if not rows:
        return False
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo("America/New_York")
        dated = []
        for row in rows[-80:]:
            timestamp = float(row.get("timestamp") or 0.0)
            if timestamp <= 0:
                continue
            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            moment = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(eastern)
            dated.append((moment, row))
        if not dated:
            return False
        target_date = dated[-1][0].date()
        session = [(moment, row) for moment, row in dated if moment.date() == target_date]
        opening = [
            row
            for moment, row in session
            if (moment.hour, moment.minute) >= (9, 30)
            and (moment.hour, moment.minute) < (10, 0)
        ]
        latest_moment, latest = session[-1]
        if len(opening) < 2 or (latest_moment.hour, latest_moment.minute) < (10, 0):
            return False
        opening_high = max(float(row["high"]) for row in opening)
        opening_low = min(float(row["low"]) for row in opening)
        return (
            float(latest["close"]) > opening_high
            if side == "long"
            else float(latest["close"]) < opening_low
        )
    except (KeyError, OSError, OverflowError, TypeError, ValueError):
        return False


def _average_true_range(rows: Sequence[Mapping[str, float]], period: int) -> float:
    values = []
    for index in range(max(1, len(rows) - period), len(rows)):
        row = rows[index]
        previous_close = float(rows[index - 1]["close"])
        values.append(
            max(
                float(row["high"]) - float(row["low"]),
                abs(float(row["high"]) - previous_close),
                abs(float(row["low"]) - previous_close),
            )
        )
    if values:
        return sum(values) / len(values)
    return max(abs(float(rows[-1]["close"])) * 1e-6, 1e-9)


def _robust_scale(values: Sequence[float], center: float) -> float:
    deviations = [abs(value - center) for value in values]
    mad = median(deviations) if deviations else 0.0
    robust = 1.4826 * mad
    if robust > 1e-9:
        return robust
    return max(_rms([value - center for value in values]), 1e-9)


def _rms(values: Sequence[float]) -> float:
    return sqrt(sum(value * value for value in values) / max(len(values), 1))


def _score_from_signed(value: float, *, scale: float) -> float:
    return 50.0 + 50.0 * _clamp(value / max(scale, 1e-9), -1.0, 1.0)


def _sigmoid(value: float) -> float:
    bounded = _clamp(value, -40.0, 40.0)
    return 1.0 / (1.0 + exp(-bounded))


def _bounded(value: Any, lower: float, upper: float, default: float) -> float:
    parsed = _finite(value, default)
    return _clamp(float(parsed), lower, upper)


def _finite(value: Any, default: float | None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


__all__ = (
    "CHANGE_POINT_FLOW_PROFILE_VERSION",
    "default_change_point_flow_config",
    "evaluate_change_point_flow_entry",
    "normalize_change_point_flow_config",
    "resolve_trend_event_candidate",
    "select_independent_change_point_flow_candidate",
)

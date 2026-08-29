from __future__ import annotations

import pytest

from utbreakout.change_point_flow import (
    CHANGE_POINT_FLOW_PROFILE_VERSION,
    evaluate_change_point_flow_entry,
    normalize_change_point_flow_config,
    resolve_trend_event_candidate,
    select_independent_change_point_flow_candidate,
)
from bot_runtime.signal_entry import build_durable_entry_plan_summary


def _event_rows(direction=1, count=45):
    price = 100.0
    rows = []
    for index in range(count):
        if index < count - 4:
            move = 0.08 if index % 2 == 0 else -0.06
            volume = 100.0
        else:
            move = 0.85 * direction
            volume = 210.0
        open_price = price
        price = max(1.0, price + move)
        rows.append({
            "timestamp": 1_700_000_000_000 + index * 900_000,
            "open": open_price,
            "high": max(open_price, price) + 0.12,
            "low": min(open_price, price) - 0.12,
            "close": price,
            "volume": volume,
        })
    return rows


@pytest.mark.parametrize(
    ("side", "direction", "context"),
    (
        (
            "long",
            1,
            {
                "rolling_orderbook_imbalance_pct": 16.0,
                "rolling_orderbook_imbalance_delta": 7.0,
                "taker_buy_sell_ratio": 1.20,
                "open_interest_delta_z": 1.2,
                "open_interest_acceleration": 0.8,
            },
        ),
        (
            "short",
            -1,
            {
                "rolling_orderbook_imbalance_pct": -16.0,
                "rolling_orderbook_imbalance_delta": -7.0,
                "taker_buy_sell_ratio": 0.82,
                "open_interest_delta_z": 1.2,
                "open_interest_acceleration": 0.8,
            },
        ),
    ),
)
def test_change_point_flow_detects_symmetric_fresh_regime(side, direction, context):
    result = evaluate_change_point_flow_entry(
        side,
        _event_rows(direction),
        futures_context=context,
        trend_metrics={
            "weighted_momentum": 0.75 * direction,
            "trend_clarity": 0.75,
            "trend_efficiency": 0.55,
            "impulse_breakout": True,
            "atr": 1.5,
        },
    )

    assert result["allowed"] is True
    assert result["profile"] == CHANGE_POINT_FLOW_PROFILE_VERSION
    assert result["state"] == "new_regime"
    assert result["risk_tier"] in {"strong", "elite"}
    assert result["initial_margin_fraction"] >= 0.70
    assert result["stop_atr_multiplier"] == pytest.approx(1.35)
    assert result["event_structure_stop"] is not None


def test_change_point_flow_rejects_measured_multi_source_opposition():
    rows = []
    price = 100.0
    for index in range(45):
        move = 0.03 if index % 2 == 0 else -0.03
        open_price = price
        price += move
        rows.append({
            "timestamp": 1_700_000_000_000 + index * 900_000,
            "open": open_price,
            "high": max(open_price, price) + 0.10,
            "low": min(open_price, price) - 0.10,
            "close": price,
            "volume": 100.0,
        })
    result = evaluate_change_point_flow_entry(
        "long",
        rows,
        futures_context={
            "rolling_orderbook_imbalance_pct": -25.0,
            "rolling_orderbook_imbalance_delta": -15.0,
            "taker_buy_sell_ratio": 0.70,
        },
        trend_metrics={"ema_crossover": True, "weighted_momentum": 0.6},
    )

    assert result["allowed"] is False
    assert result["code"] == "REJECTED_CHANGE_POINT_FLOW_CONFLICT"
    assert result["flow_source_count"] == 3


def test_stale_orderflow_is_excluded_instead_of_vetoing_fresh_price_regime():
    result = evaluate_change_point_flow_entry(
        "long",
        _event_rows(1),
        futures_context={
            "orderflow_snapshot_ts": 1.0,
            "rolling_orderbook_imbalance_pct": -25.0,
            "rolling_orderbook_imbalance_delta": -15.0,
            "taker_buy_sell_ratio": 0.70,
        },
    )

    assert result["allowed"] is True
    assert result["orderflow_stale"] is True
    assert result["flow_source_count"] == 0
    assert result["flow_score"] == pytest.approx(50.0)


def test_missing_derivatives_data_degrades_to_fresh_price_shape():
    result = evaluate_change_point_flow_entry(
        "long",
        _event_rows(1),
        futures_context={},
        trend_metrics={
            "pullback_resumption": True,
            "weighted_momentum": 0.55,
            "trend_clarity": 0.60,
        },
    )

    assert result["allowed"] is True
    assert result["flow_source_count"] == 0
    assert result["paths"]["fresh_trend_shape"] is True


def test_established_high_quality_trend_is_an_alternative_path():
    result = evaluate_change_point_flow_entry(
        "short",
        _event_rows(-1, count=12),
        futures_context={},
        trend_metrics={
            "weighted_momentum": -0.80,
            "trend_clarity": 0.70,
            "trend_efficiency": 0.55,
        },
    )

    assert result["allowed"] is True
    assert result["state"] == "established_trend"
    assert result["price_evidence_available"] is False


def test_change_point_flow_configuration_is_bounded_and_monotonic():
    cfg = normalize_change_point_flow_config({
        "recent_bars": 1,
        "baseline_bars": 500,
        "base_initial_margin_fraction": 0.90,
        "strong_initial_margin_fraction": 0.40,
        "elite_initial_margin_fraction": 0.30,
    })

    assert cfg["recent_bars"] == 3
    assert cfg["baseline_bars"] == 96
    assert cfg["base_initial_margin_fraction"] == pytest.approx(0.80)
    assert cfg["strong_initial_margin_fraction"] >= cfg["base_initial_margin_fraction"]
    assert cfg["elite_initial_margin_fraction"] >= cfg["strong_initial_margin_fraction"]


def test_change_point_flow_audit_fields_survive_entry_plan_persistence():
    summary = build_durable_entry_plan_summary({
        "strategy": "adaptive_breakout_trend_v1",
        "change_point_flow_profile": CHANGE_POINT_FLOW_PROFILE_VERSION,
        "change_point_flow_state": "new_regime",
        "change_point_flow_total_score": 87.5,
        "change_point_flow_stop_atr_multiplier": 1.35,
        "change_point_flow_orderflow_age_seconds": 12.5,
        "change_point_flow_orderflow_stale": False,
        "trend_event_candidate_source": "event_only",
        "trend_event_candidate_agreement": "event_only",
        "trend_event_candidate_score": 87.5,
        "independent_event_context_fast_ema_distance_atr": 0.8,
        "independent_event_context_max_fast_ema_distance_atr": 1.5,
        "independent_event_risk_tier_cap": "base",
        "independent_event_risk_tier_capped": True,
        "temporary_debug_value": "drop-me",
    })

    assert summary["change_point_flow_profile"] == CHANGE_POINT_FLOW_PROFILE_VERSION
    assert summary["change_point_flow_state"] == "new_regime"
    assert summary["change_point_flow_total_score"] == pytest.approx(87.5)
    assert summary["trend_event_candidate_source"] == "event_only"
    assert summary["trend_event_candidate_agreement"] == "event_only"
    assert summary["trend_event_candidate_score"] == pytest.approx(87.5)
    assert summary["change_point_flow_orderflow_age_seconds"] == pytest.approx(12.5)
    assert summary["change_point_flow_orderflow_stale"] is False
    assert summary["independent_event_context_fast_ema_distance_atr"] == pytest.approx(0.8)
    assert summary["independent_event_risk_tier_cap"] == "base"
    assert summary["independent_event_risk_tier_capped"] is True
    assert "temporary_debug_value" not in summary


@pytest.mark.parametrize(("side", "direction"), (("long", 1), ("short", -1)))
def test_independent_event_engine_creates_direction_without_trend_gate(side, direction):
    context = {
        "rolling_orderbook_imbalance_pct": 18.0 * direction,
        "rolling_orderbook_imbalance_delta": 9.0 * direction,
        "taker_buy_sell_ratio": 1.25 if direction > 0 else 0.78,
        "open_interest_delta_z": 1.4,
        "open_interest_acceleration": 0.9,
    }
    result = select_independent_change_point_flow_candidate(
        _event_rows(direction),
        futures_context=context,
    )

    assert result["allowed"] is True
    assert result["side"] == side
    assert result["decision"]["state"] in {"new_regime", "persistent_flow"}


def test_independent_event_engine_does_not_use_legacy_missing_data_fallback():
    result = select_independent_change_point_flow_candidate([], futures_context={})

    assert result["allowed"] is False
    assert result["side"] is None


def test_candidate_resolver_keeps_trend_and_event_as_or_paths():
    trend_only = resolve_trend_event_candidate(
        {
            "allowed": True,
            "side": "long",
            "score": 72.0,
            "fresh_continuation": True,
        },
        {"allowed": False},
    )
    event_only = resolve_trend_event_candidate(
        {"allowed": False},
        {
            "allowed": True,
            "side": "short",
            "score": 78.0,
            "decision": {
                "state": "new_regime",
                "risk_tier": "strong",
            },
        },
    )
    aligned = resolve_trend_event_candidate(
        {
            "allowed": True,
            "side": "long",
            "score": 72.0,
            "fresh_continuation": True,
        },
        {"allowed": True, "side": "long", "score": 80.0},
    )

    assert trend_only["source"] == "trend_only"
    assert event_only["source"] == "event_only"
    assert event_only["event_state"] == "new_regime"
    assert event_only["event_risk_tier"] == "strong"
    assert aligned["source"] == "aligned"
    assert aligned["score"] > max(72.0, 80.0)
    assert trend_only["fresh_continuation"] is True
    assert aligned["fresh_continuation"] is True


def test_candidate_resolver_waits_when_event_opposes_actionable_trend():
    waiting = resolve_trend_event_candidate(
        {"allowed": True, "side": "long", "score": 76.0},
        {"allowed": True, "side": "short", "score": 70.0},
        conflict_margin=12.0,
    )
    event_winner = resolve_trend_event_candidate(
        {"allowed": True, "side": "long", "score": 65.0},
        {"allowed": True, "side": "short", "score": 82.0},
        conflict_margin=12.0,
    )

    assert waiting["allowed"] is False
    assert waiting["source"] == "conflict_wait"
    assert event_winner["allowed"] is False
    assert event_winner["side"] is None
    assert event_winner["source"] == "conflict_wait"


def test_candidate_resolver_requires_explicit_opt_in_for_event_conflict_override():
    event_winner = resolve_trend_event_candidate(
        {"allowed": True, "side": "long", "score": 65.0},
        {"allowed": True, "side": "short", "score": 82.0},
        conflict_margin=12.0,
        allow_event_conflict_override=True,
    )

    assert event_winner["allowed"] is True
    assert event_winner["side"] == "short"
    assert event_winner["source"] == "event_conflict_winner"

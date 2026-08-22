from __future__ import annotations

import pytest

from utbreakout.change_point_flow import (
    CHANGE_POINT_FLOW_PROFILE_VERSION,
    evaluate_change_point_flow_entry,
    normalize_change_point_flow_config,
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
        "temporary_debug_value": "drop-me",
    })

    assert summary["change_point_flow_profile"] == CHANGE_POINT_FLOW_PROFILE_VERSION
    assert summary["change_point_flow_state"] == "new_regime"
    assert summary["change_point_flow_total_score"] == pytest.approx(87.5)
    assert "temporary_debug_value" not in summary

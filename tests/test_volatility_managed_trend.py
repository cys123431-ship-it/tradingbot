import math

import pytest

from utbreakout.volatility_managed_trend import (
    VOLATILITY_MANAGED_TREND_STRATEGY,
    default_volatility_managed_trend_config,
    evaluate_volatility_managed_trend,
)


def _trend_rows(direction=1, count=130):
    rows = []
    previous = 100.0
    for index in range(count):
        drift = direction * (0.16 + 0.04 * math.sin(index / 5.0))
        close = previous + drift
        rows.append({
            "timestamp": index * 3_600_000,
            "open": previous,
            "high": max(previous, close) + 0.42,
            "low": min(previous, close) - 0.42,
            "close": close,
            "volume": 120.0 if index == count - 1 else 100.0 + index % 7,
        })
        previous = close
    return rows


def _healthy_l2(side):
    return {
        "allowed": True,
        "state": "bid_support" if side == "long" else "ask_pressure",
        "direction_support": side,
        "risk_multiplier": 0.9,
    }


@pytest.mark.parametrize(("direction", "side"), [(1, "long"), (-1, "short")])
def test_vmt_accepts_persistent_multi_horizon_trend(direction, side):
    decision = evaluate_volatility_managed_trend(_trend_rows(direction), _healthy_l2(side))

    assert decision.allowed is True
    assert decision.side == side
    assert decision.score >= default_volatility_managed_trend_config()["score_min"]
    assert 0.0 < decision.risk_multiplier <= 0.60
    assert decision.metrics["required_votes"] == 2
    assert sum(value == side for value in decision.metrics["horizon_votes"].values()) >= 2


def test_vmt_rejects_choppy_path_even_when_last_close_is_higher():
    rows = []
    for index in range(130):
        close = 100.0 + (1.0 if index % 2 else -1.0) + index * 0.005
        rows.append({
            "timestamp": index * 3_600_000,
            "open": 100.0,
            "high": close + 0.6,
            "low": close - 0.6,
            "close": close,
            "volume": 100.0,
        })

    decision = evaluate_volatility_managed_trend(rows, _healthy_l2("long"))

    assert decision.allowed is False
    assert decision.reason in {
        "multi_horizon_trend_not_aligned",
        "ema_trend_not_aligned",
        "trend_efficiency_too_low",
    }


def test_vmt_fails_closed_when_shared_l2_is_stressed():
    decision = evaluate_volatility_managed_trend(
        _trend_rows(1),
        {"allowed": False, "state": "stressed", "risk_multiplier": 0.0},
    )

    assert decision.allowed is False
    assert decision.side == "long"
    assert decision.reason == "l2_stressed"


def test_vmt_rejects_immediate_adverse_return_shock_inside_old_trend():
    rows = [dict(row) for row in _trend_rows(1)]
    previous = rows[-2]["close"]
    rows[-1].update({
        "open": previous,
        "high": previous + 0.10,
        "low": previous - 0.30,
        "close": previous - 0.20,
        "volume": 110.0,
    })

    decision = evaluate_volatility_managed_trend(rows, _healthy_l2("long"))

    assert decision.allowed is False
    assert decision.reason == "latest_bar_reversal_shock"
    assert decision.metrics["latest_adverse_return_sigma"] > 1.35


def test_vmt_volatility_targeting_can_scale_below_quality_floor():
    decision = evaluate_volatility_managed_trend(
        _trend_rows(1),
        _healthy_l2("long"),
        {"target_hourly_volatility": 0.0001},
    )

    assert decision.allowed is True
    assert 0.0 < decision.risk_multiplier < default_volatility_managed_trend_config()["risk_multiplier_floor"]
    assert decision.metrics["volatility_scale"] < 1.0


def test_vmt_identifier_is_stable():
    assert VOLATILITY_MANAGED_TREND_STRATEGY == "volatility_managed_trend_v1"

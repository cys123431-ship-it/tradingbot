from __future__ import annotations

from pathlib import Path

import pytest

from utbreakout.adaptive_breakout_trend import (
    ADAPTIVE_BREAKOUT_TREND_STRATEGY,
    default_adaptive_breakout_trend_config,
    evaluate_adaptive_breakout_trend,
)
from utbreakout.dynamic_leverage import select_dynamic_leverage


ROOT = Path(__file__).resolve().parents[1]
CALM_L2 = {
    "allowed": True,
    "risk_multiplier": 1.0,
    "state": "calm",
    "direction_support": "",
}


def _trend_rows(direction: int, count: int = 220) -> list[dict]:
    rows = []
    price = 100.0
    for index in range(count):
        step = (0.18 * direction) + (0.04 if index % 5 == 0 else -0.02)
        open_price = price
        price = max(5.0, price + step)
        rows.append(
            {
                "timestamp": index * 3_600_000,
                "open": open_price,
                "high": max(open_price, price) + 0.12,
                "low": min(open_price, price) - 0.12,
                "close": price,
                "volume": 1_000.0 + index,
            }
        )
    rows[-1]["close"] += 0.50 * direction
    if direction > 0:
        rows[-1]["high"] = rows[-1]["close"] + 0.10
    else:
        rows[-1]["low"] = rows[-1]["close"] - 0.10
    return rows


@pytest.mark.parametrize(("direction", "side"), ((1, "long"), (-1, "short")))
def test_multi_horizon_breakout_accepts_both_directions(direction, side):
    decision = evaluate_adaptive_breakout_trend(_trend_rows(direction), CALM_L2)

    assert decision.allowed is True
    assert decision.side == side
    assert decision.score >= default_adaptive_breakout_trend_config()["score_min"]
    assert 0.60 <= decision.risk_multiplier <= 1.0
    assert decision.metrics["fresh_breakout"] is True
    assert sum(value == side for value in decision.metrics["horizon_votes"].values()) >= 2


def test_l2_stress_is_a_hard_safety_gate():
    decision = evaluate_adaptive_breakout_trend(
        _trend_rows(1),
        {"allowed": False, "risk_multiplier": 0.0, "state": "stressed_thin"},
    )

    assert decision.allowed is False
    assert decision.side == "long"
    assert decision.reason == "l2_stressed"


def test_volatility_shock_is_rejected_before_sizing():
    cfg = {"volatility_shock_ratio": 1.01}
    decision = evaluate_adaptive_breakout_trend(_trend_rows(1), CALM_L2, cfg)

    assert decision.allowed is False
    assert decision.reason == "volatility_shock"


def test_strong_standalone_signal_can_reach_high_dynamic_leverage():
    plan = {
        "strategy": ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        "side": "long",
        "entry_price": 100.0,
        "risk_distance": 1.0,
        "atr": 1.0,
        "adaptive_breakout_trend_score": 95.0,
        "market_quality_risk_multiplier": 1.0,
        "l2_gate": CALM_L2,
        "l2_state": "calm",
        "l2_risk_multiplier": 1.0,
    }

    decision = select_dynamic_leverage(plan)

    assert decision.quality_source == "adaptive_breakout_trend_score"
    assert decision.tier == "strong_single"
    assert decision.leverage == 8


def test_dedicated_telegram_mode_is_separate_and_mutually_exclusive():
    setup_source = (ROOT / "bot_runtime" / "controller_telegram_setup.py").read_text(
        encoding="utf-8"
    )
    keyboard_source = (ROOT / "bot_runtime" / "controller_telegram.py").read_text(
        encoding="utf-8"
    )

    assert 'KeyboardButton("/trend")' in keyboard_source
    assert "callback_data='trend:on'" in setup_source
    assert "callback_data='trend:off'" in setup_source
    assert "callback_data='trend:status'" in setup_source
    assert "'quad_alpha_enabled_strategies'],\n                []," in setup_source
    assert "reset_exit_cache=False" in setup_source


def test_dispatch_and_registry_include_standalone_strategy():
    candle_source = (ROOT / "bot_runtime" / "signal_candles.py").read_text(
        encoding="utf-8"
    )
    registry_source = (ROOT / "bot_runtime" / "strategy_registry.py").read_text(
        encoding="utf-8"
    )

    assert "active_strategy == ADAPTIVE_BREAKOUT_TREND_STRATEGY" in candle_source
    assert "_calculate_adaptive_breakout_trend_signal" in candle_source
    assert "ADAPTIVE_BREAKOUT_TREND_STRATEGY," in registry_source

from types import SimpleNamespace

import pytest

from utbreakout.direction_engine import evaluate_direction_engine
from utbreakout.alpha_engine import evaluate_profit_alpha


def test_direction_engine_allows_aligned_long_context():
    decision = evaluate_direction_engine(
        side="long",
        values={"momentum_alignment": "long"},
        trend=76.0,
        relative=72.0,
        regime=74.0,
        derivatives=70.0,
        squeeze=52.0,
        ev_decision=SimpleNamespace(score=78.0, mtf_alignment="3/3"),
    )

    assert decision.allowed is True
    assert decision.score >= decision.min_score
    assert any("direction trend" in item for item in decision.reasons)


def test_direction_engine_blocks_weak_mtf_and_opposite_momentum():
    decision = evaluate_direction_engine(
        side="short",
        values={"momentum_alignment": "long"},
        trend=62.0,
        relative=58.0,
        regime=60.0,
        derivatives=61.0,
        squeeze=45.0,
        ev_decision=SimpleNamespace(score=60.0, mtf_alignment="1/3"),
    )

    assert decision.allowed is False
    assert "weak MTF alignment 1/3" in decision.blockers
    assert "momentum against" in decision.blockers


def test_direction_engine_raises_threshold_against_top_regime():
    neutral = evaluate_direction_engine(
        side="long",
        values={},
        trend=66.0,
        relative=63.0,
        regime=62.0,
        derivatives=62.0,
        squeeze=48.0,
        config={
            "direction_engine_min_score": 62.0,
            "direction_engine_opposite_regime_min_score": 68.0,
        },
        opposite_regime=False,
    )
    opposite = evaluate_direction_engine(
        side="long",
        values={},
        trend=66.0,
        relative=63.0,
        regime=62.0,
        derivatives=62.0,
        squeeze=48.0,
        config={
            "direction_engine_min_score": 62.0,
            "direction_engine_opposite_regime_min_score": 68.0,
        },
        opposite_regime=True,
    )

    assert neutral.min_score == 62.0
    assert neutral.allowed is True
    assert opposite.min_score == 68.0
    assert opposite.allowed is False
    assert any("direction engine" in item for item in opposite.blockers)


def test_profit_alpha_uses_independent_direction_engine_components():
    decision = evaluate_profit_alpha(
        side="long",
        values={
            "adx": 32.0,
            "plus_di": 28.0,
            "minus_di": 12.0,
            "close": 110.0,
            "ema50": 100.0,
            "ema200": 95.0,
            "htf_close": 110.0,
            "htf_ema_slow": 100.0,
            "volume_ratio": 1.55,
            "range_expansion": 1.25,
            "coin_selector_score": 82.0,
            "trend_health_score": 84.0,
            "strategy_quality_score": 78.0,
            "quality_score_v2_score": 81.0,
            "taker_buy_sell_ratio": 1.08,
            "rolling_ofi": 18.0,
            "orderbook_imbalance_pct": 14.0,
            "open_interest_change_1h": 0.48,
            "funding_rate": -0.0001,
            "basis_pct": 0.02,
            "market_regime_context": {
                "items": {
                    "BTC/USDT": {"direction": "long", "return_lookback_pct": 0.8},
                    "ETH/USDT": {"direction": "long", "return_lookback_pct": 0.6},
                }
            },
        },
        ev_decision=SimpleNamespace(
            allowed=True,
            mode="STRONG_TREND",
            score=78.0,
            win_probability=0.58,
            risk_multiplier=0.82,
            mtf_alignment="3/3",
            signal_age_candles=3.0,
            reacceleration=True,
            reasons=("EV trend edge",),
            blockers=(),
        ),
    )

    assert decision.direction_score == pytest.approx(decision.components["direction"], abs=0.0001)
    assert decision.components["direction_min_score"] >= 62.0
    assert "direction_risk_multiplier" in decision.components

from types import SimpleNamespace

from utbreakout.continuation_entry import evaluate_trend_continuation_entry


def _base_values():
    return {
        "entry_price": 100.0,
        "open": 99.5,
        "ema50": 99.0,
        "ema50_prev": 98.5,
        "vwap": 99.2,
        "bb_mid": 99.1,
        "bb_upper": 101.0,
        "donchian_high_prev": 100.5,
        "keltner_upper": 101.2,
        "atr_pct": 0.8,
        "volume_ratio": 0.62,
        "range_expansion_ratio": 1.08,
        "adx": 18.0,
        "plus_di": 25.0,
        "minus_di": 18.0,
    }


def test_long_trend_continuation_accepts_bias_state():
    direction = SimpleNamespace(
        long_allowed=True,
        short_allowed=False,
        size_multiplier=0.75,
        reason="regime=bullish",
    )
    decision = evaluate_trend_continuation_entry(
        side="long",
        candidate_type="bias_state",
        cfg={},
        values=_base_values(),
        direction_decision=direction,
        quality_score_v2={"score": 55.0},
        trend_health={"score": 50.0},
        strategy_quality={"score": 48.0},
    )
    assert decision.accepted is True
    assert decision.mode == "trend_continuation"
    assert decision.risk_multiplier > 0
    assert any("setup" in item for item in decision.positives)


def test_trend_continuation_blocks_extreme_low_volume():
    values = _base_values()
    values["volume_ratio"] = 0.20
    direction = SimpleNamespace(
        long_allowed=True,
        short_allowed=False,
        size_multiplier=1.0,
        reason="regime=bullish",
    )
    decision = evaluate_trend_continuation_entry(
        side="long",
        candidate_type="bias_state",
        cfg={},
        values=values,
        direction_decision=direction,
        quality_score_v2={"score": 55.0},
        trend_health={"score": 50.0},
        strategy_quality={"score": 48.0},
    )
    assert decision.accepted is False
    assert any("volume extremely weak" in item for item in decision.blockers)


def test_short_trend_continuation_requires_direction_allowed():
    values = _base_values()
    values.update({
        "entry_price": 98.0,
        "open": 98.5,
        "ema50": 99.0,
        "ema50_prev": 99.5,
        "bb_lower": 97.5,
        "donchian_low_prev": 98.5,
        "keltner_lower": 97.8,
        "plus_di": 16.0,
        "minus_di": 25.0,
    })
    direction = SimpleNamespace(
        long_allowed=True,
        short_allowed=False,
        size_multiplier=1.0,
        reason="regime=bullish short disabled",
    )
    decision = evaluate_trend_continuation_entry(
        side="short",
        candidate_type="bias_state",
        cfg={},
        values=values,
        direction_decision=direction,
        quality_score_v2={"score": 60.0},
        trend_health={"score": 55.0},
        strategy_quality={"score": 50.0},
    )
    assert decision.accepted is False
    assert any("direction blocked" in item for item in decision.blockers)

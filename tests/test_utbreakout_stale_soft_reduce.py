import pytest

import emas


def _engine_and_passing_values():
    engine = object.__new__(emas.SignalEngine)
    values = {
        "entry_price": 100.0,
        "open": 99.6,
        "ema50": 99.7,
        "ema50_prev": 99.4,
        "vwap": 99.8,
        "bb_mid": 99.6,
        "adx": 28.0,
        "plus_di": 31.0,
        "minus_di": 16.0,
        "atr_pct": 0.8,
        "volume_ratio": 1.2,
        "range_expansion_ratio": 1.1,
        "donchian_high_prev": 103.0,
        "keltner_upper": 101.0,
        "bb_upper": 102.0,
        "htf_ready": True,
        "htf_close": 100.0,
        "htf_ema_fast": 99.0,
        "htf_ema_slow": 98.0,
    }
    return engine, values


def test_stale_signal_multiplier_steps():
    cfg = emas.apply_profit_opportunity_effective_overrides({})

    assert emas._utbreakout_stale_signal_multiplier(cfg, 10, 10) == 1.0
    assert emas._utbreakout_stale_signal_multiplier(cfg, 11, 10) == 0.75
    assert emas._utbreakout_stale_signal_multiplier(cfg, 20, 10) == 0.65
    assert emas._utbreakout_stale_signal_multiplier(cfg, 40, 10) == 0.55
    assert emas._utbreakout_stale_signal_multiplier(cfg, 79, 10) == 0.45


def test_ut_signal_stale_is_soft_reduce_not_block():
    engine, values = _engine_and_passing_values()
    cfg = emas.apply_profit_opportunity_effective_overrides({
        "entry_timeframe": "15m",
        "adaptive_timeframe_enabled": True,
        "bias_continuation_stale_soft_reduce_enabled": True,
        "bias_continuation_max_signal_age_candles": 10,
        "bias_continuation_15m_max_signal_age_candles": 10,
    })

    result = engine._evaluate_utbreakout_bias_continuation(
        "long",
        cfg,
        {
            "candidate_type": "bias_state",
            "decision_candle_ts": 80 * 900_000,
            "ut_signal_ts": 1 * 900_000,
            "adaptive_timeframe_decision": {"selected_score": 70.0},
        },
        values,
        {"id": 22},
    )

    assert result["state"] == "reduced"
    assert 0.0 < result["risk_multiplier"] < 1.0
    assert "UT signal stale REDUCE x0.45: 79.0>10 candles" in result["summary"]
    assert "BLOCK: UT signal stale" not in result["summary"]


def test_short_ut_signal_stale_is_also_soft_reduce_not_block():
    engine, values = _engine_and_passing_values()
    values.update({
        "open": 100.4,
        "ema50": 100.3,
        "ema50_prev": 100.6,
        "vwap": 100.2,
        "bb_mid": 100.4,
        "plus_di": 16.0,
        "minus_di": 31.0,
        "donchian_low_prev": 97.0,
        "keltner_lower": 99.0,
        "bb_lower": 98.0,
        "htf_ema_fast": 101.0,
        "htf_ema_slow": 102.0,
    })
    cfg = emas.apply_profit_opportunity_effective_overrides({
        "entry_timeframe": "15m",
        "adaptive_timeframe_enabled": True,
    })

    result = engine._evaluate_utbreakout_bias_continuation(
        "short",
        cfg,
        {
            "candidate_type": "bias_state",
            "decision_candle_ts": 80 * 900_000,
            "ut_signal_ts": 1 * 900_000,
            "adaptive_timeframe_decision": {"selected_score": 70.0},
        },
        values,
        {"id": 22},
    )

    assert result["state"] == "reduced"
    assert result["risk_multiplier"] == pytest.approx(0.225)
    assert "UT signal stale REDUCE x0.45: 79.0>10 candles" in result["summary"]
    assert "BLOCK: UT signal stale" not in result["summary"]


def test_stale_signal_can_keep_legacy_hard_block_when_soft_reduce_disabled():
    engine, values = _engine_and_passing_values()
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg.update({
        "entry_timeframe": "15m",
        "adaptive_timeframe_enabled": True,
        "bias_continuation_stale_soft_reduce_enabled": False,
    })

    result = engine._evaluate_utbreakout_bias_continuation(
        "long",
        cfg,
        {
            "candidate_type": "bias_state",
            "decision_candle_ts": 80 * 900_000,
            "ut_signal_ts": 1 * 900_000,
            "adaptive_timeframe_decision": {"selected_score": 70.0},
        },
        values,
        {"id": 22},
    )

    assert result["state"] is False
    assert result["risk_multiplier"] == 0.0
    assert "BLOCK: UT signal stale 79.0>10 candles" in result["summary"]


@pytest.mark.parametrize("raw_multiplier", [0.1845, 0.01])
def test_ev_profile_does_not_reinflate_soft_reductions(raw_multiplier):
    cfg = emas.apply_profit_opportunity_effective_overrides({})

    assert emas._apply_utbreakout_risk_multiplier_floor(raw_multiplier, cfg) == raw_multiplier
    assert emas._apply_utbreakout_risk_multiplier_floor(0.0, cfg) == 0.0

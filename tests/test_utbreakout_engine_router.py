import utbreakout.engine_router as router
from utbreakout.engine_router import (
    AlphaSignal,
    EngineStats,
    LadderTP,
    build_adaptive_ladder_tp,
    evaluate_alpha_engines,
    evaluate_exhaustion_reversal_long,
    evaluate_final_trade_decision,
    evaluate_trend_continuation_long,
    should_disable_engine,
)
from utbreakout.market_context import MarketContext


def _trend_long_context(**overrides):
    data = {
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "open": 104.0,
        "high": 106.0,
        "low": 103.0,
        "close": 105.0,
        "volume": 1500.0,
        "utbot_direction": "LONG",
        "donchian_high": 104.0,
        "donchian_low": 96.0,
        "donchian_mid": 100.0,
        "donchian_high_previous": 104.0,
        "donchian_low_previous": 96.0,
        "adx": 32.0,
        "plus_di": 35.0,
        "minus_di": 12.0,
        "htf_trend": "UP",
        "supertrend_direction": "UP",
        "atr": 2.0,
        "atr_percentile": 50.0,
        "volume_ratio": 1.6,
        "range_expansion": 1.2,
        "squeeze_percentile": 30.0,
        "spread_bps": 1.0,
    }
    data.update(overrides)
    return MarketContext(**data)


def test_alpha_engine_rejects_long_short_conflict(monkeypatch):
    monkeypatch.setitem(
        router.ENGINE_EVALUATORS,
        "TREND_CONTINUATION_LONG",
        lambda context, config=None, stats=None: AlphaSignal(True, "LONG", "TREND_CONTINUATION_LONG", 0.8, 0.4, 1.0, []),
    )
    monkeypatch.setitem(
        router.ENGINE_EVALUATORS,
        "TREND_CONTINUATION_SHORT",
        lambda context, config=None, stats=None: AlphaSignal(True, "SHORT", "TREND_CONTINUATION_SHORT", 0.8, 0.4, 1.0, []),
    )

    signal = evaluate_alpha_engines(
        _trend_long_context(),
        {"enabled_engines": ["TREND_CONTINUATION_LONG", "TREND_CONTINUATION_SHORT"]},
    )

    assert signal.valid is False
    assert signal.engine == "CONFLICT"
    assert "LONG_SHORT_CONFLICT" in signal.reasons


def test_alpha_engine_selects_best_risk_adjusted_signal(monkeypatch):
    monkeypatch.setitem(
        router.ENGINE_EVALUATORS,
        "TREND_CONTINUATION_LONG",
        lambda context, config=None, stats=None: AlphaSignal(True, "LONG", "TREND_CONTINUATION_LONG", 0.7, 0.3, 1.0, []),
    )
    monkeypatch.setitem(
        router.ENGINE_EVALUATORS,
        "SQUEEZE_BREAKOUT_LONG",
        lambda context, config=None, stats=None: AlphaSignal(True, "LONG", "SQUEEZE_BREAKOUT_LONG", 0.8, 0.5, 0.8, []),
    )

    signal = evaluate_alpha_engines(
        _trend_long_context(),
        {"enabled_engines": ["TREND_CONTINUATION_LONG", "SQUEEZE_BREAKOUT_LONG"]},
    )

    assert signal.engine == "SQUEEZE_BREAKOUT_LONG"


def test_alpha_engine_rejects_low_confidence(monkeypatch):
    monkeypatch.setitem(
        router.ENGINE_EVALUATORS,
        "TREND_CONTINUATION_LONG",
        lambda context, config=None, stats=None: AlphaSignal(True, "LONG", "TREND_CONTINUATION_LONG", 0.5, 0.4, 1.0, []),
    )

    signal = evaluate_alpha_engines(
        _trend_long_context(),
        {"enabled_engines": ["TREND_CONTINUATION_LONG"], "min_alpha_confidence": 0.55},
    )

    assert signal.valid is False
    assert signal.engine == "LOW_CONFIDENCE"


def test_alpha_engine_returns_none_when_no_engine_valid():
    signal = evaluate_alpha_engines(_trend_long_context(volume_ratio=0.5), {"engine": "TREND_CONTINUATION_LONG"})

    assert signal.valid is False
    assert "NO_ENGINE_SIGNAL" in signal.reasons


def test_trend_long_requires_htf_alignment():
    signal = evaluate_trend_continuation_long(_trend_long_context(htf_trend="DOWN"))

    assert signal.valid is False
    assert "TREND_LONG_CONDITIONS_NOT_MET" in signal.reasons


def test_exhaustion_reversal_requires_derivatives_data():
    signal = evaluate_exhaustion_reversal_long(_trend_long_context())

    assert signal.valid is False
    assert "MISSING_DERIVATIVES_DATA" in signal.reasons


def test_ladder_tp_strong_signal_uses_tp3():
    ladder = build_adaptive_ladder_tp(
        _trend_long_context(),
        AlphaSignal(True, "LONG", "TREND_CONTINUATION_LONG", 0.8, 0.5, 1.0, []),
    )

    assert isinstance(ladder, LadderTP)
    assert ladder.tp3_r == 3.0
    assert sum(target["pct"] for target in ladder.targets()) == 100.0


def test_engine_disabled_negative_expectancy_after_min_trades():
    assert should_disable_engine(EngineStats(trade_count=30, expectancy_r=-0.01, profit_factor=1.2)) is True


def test_final_decision_uses_adaptive_ladder_tp_and_never_enables_runner():
    decision = evaluate_final_trade_decision(
        {},
        _trend_long_context(),
        {
            "advanced_alpha_engine_enabled": True,
            "adaptive_ladder_tp_enabled": True,
            "engine": "TREND_CONTINUATION_LONG",
            "risk_per_trade_pct": 0.5,
            "meta_label_gate_enabled": False,
        },
    )

    assert decision.valid is True
    assert decision.side == "LONG"
    assert decision.ladder_tp is not None
    assert decision.exit_policy_name == "ADAPTIVE_LADDER_TP"
    assert decision.runner_enabled is False

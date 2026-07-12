import asyncio
import inspect
from types import SimpleNamespace

import emas


def _decision(symbol, side, ready, reason, **logs):
    return SimpleNamespace(
        symbol=symbol,
        side=side,
        entry_ready=ready,
        reason=reason,
        logs=logs,
    )


def _engine(decisions):
    engine = object.__new__(emas.SignalEngine)
    params = {
        "active_strategy": emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    }
    engine.get_runtime_strategy_params = lambda: params
    engine._get_utbot_filtered_breakout_config = lambda strategy_params: {}
    engine._relative_strength_pullback_runtime_config = lambda cfg: {
        "relative_strength_pullback_trend_live_enabled": True,
        "signal_tf": "4h",
        "trend_htf": "1d",
        "entry_execution": "next_open",
    }

    async def evaluate_candidates(**kwargs):
        return decisions, {}, {}, [item.symbol for item in decisions], {}

    engine._evaluate_relative_strength_pullback_candidates = evaluate_candidates
    return engine


def test_rspt_status_source_contract_uses_new_header():
    source = inspect.getsource(
        emas.SignalEngine.build_relative_strength_pullback_status_text
    )

    assert "📊 RSPT 전략 상태" in source
    assert "RelativeStrengthPullbackTrend status" not in source
    assert "Scanner candidates evaluated" not in source


def test_rspt_status_uses_traffic_lights_and_korean_explanations():
    decisions = [
        _decision(
            "GREEN/USDT:USDT",
            "long",
            True,
            "breakout_continuation_confirmed",
            setup_type="breakout_continuation",
            size_multiplier=1.0,
        ),
        _decision(
            "YELLOW/USDT:USDT",
            "short",
            True,
            "trend_pullback_confirmed",
            setup_type="trend_pullback",
            size_multiplier=0.60,
            size_reduction_reasons=["adx_weak_size_reduced"],
        ),
        _decision(
            "RED/USDT:USDT",
            "long",
            False,
            "relative_strength_hard_block",
            size_multiplier=1.0,
        ),
        _decision(
            "WHITE/USDT:USDT",
            None,
            False,
            "no_ut_direction",
            size_multiplier=1.0,
        ),
    ]

    text = asyncio.run(
        _engine(decisions).build_relative_strength_pullback_status_text()
    )

    assert "📊 RSPT 전략 상태" in text
    assert (
        "상태 요약: 🟢 진입 가능 1 | 🟡 수량 감소 1 | "
        "🔴 진입 차단 1 | ⚪ 신호 대기 1"
    ) in text
    assert "🟢 GREEN/USDT:USDT — LONG 진입 가능" in text
    assert "🟡 YELLOW/USDT:USDT — SHORT 조건부 진입" in text
    assert "수량/증거금 60% (x0.60)" in text
    assert "감소 이유: ADX 약세" in text
    assert "🔴 RED/USDT:USDT — LONG 진입 차단" in text
    assert "이유: 상대강도 기준 미달" in text
    assert "⚪ WHITE/USDT:USDT — UT 방향 대기" in text
    assert "UT 방향 확인 후 RSPT 조건 검사" in text
    assert "RelativeStrengthPullbackTrend status" not in text
    assert "Scanner candidates evaluated" not in text


def test_rspt_status_keeps_waiting_reasons_white():
    decisions = [
        _decision(
            "STALE/USDT:USDT",
            "long",
            False,
            "stale_signal",
            size_multiplier=1.0,
        ),
        _decision(
            "SETUP/USDT:USDT",
            "short",
            False,
            "no_entry_setup",
            size_multiplier=1.0,
        ),
    ]

    text = asyncio.run(
        _engine(decisions).build_relative_strength_pullback_status_text()
    )

    assert "🟢 진입 가능 0" in text
    assert "🟡 수량 감소 0" in text
    assert "🔴 진입 차단 0" in text
    assert "⚪ 신호 대기 2" in text
    assert "⚪ STALE/USDT:USDT — LONG 신호 대기" in text
    assert "신호가 오래되어 새 신호 대기" in text
    assert "⚪ SETUP/USDT:USDT — SHORT 신호 대기" in text
    assert "RSPT 진입 패턴 대기" in text


def test_rspt_status_requests_real_shared_ut_directions():
    engine = _engine([])
    captured = {}

    async def evaluate_candidates(**kwargs):
        captured.update(kwargs)
        return [], {}, {}, [], {}

    engine._evaluate_relative_strength_pullback_candidates = evaluate_candidates
    asyncio.run(engine.build_relative_strength_pullback_status_text())

    assert captured["resolve_ut_directions"] is True
    assert captured["direction_consumer"] == "RSPT_STATUS"
    assert captured["record_state"] is False


def test_rspt_status_explains_4h_1d_direction_conflict():
    decision = _decision(
        "CONFLICT/USDT:USDT",
        None,
        False,
        "no_ut_direction",
        size_multiplier=1.0,
        ut_direction_reason="UT 방향 불일치: 4h LONG / 1d SHORT",
        ut_direction_4h_side="long",
        ut_direction_1d_side="short",
    )

    text = asyncio.run(
        _engine([decision]).build_relative_strength_pullback_status_text()
    )

    assert "UT 방향 불일치: 4h LONG / 1d SHORT" in text
    assert "4h LONG / 1d SHORT" in text

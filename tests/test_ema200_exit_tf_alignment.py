"""EMA200 entries are skipped when the selected exit-timeframe UT opposes them.

Exits wait for a fresh opposite UT signal after entry.  An entry taken while
that UT is already opposite could not exit until the UT flipped twice (the
2026-09-28 HYPE SHORT case), so the entry itself is skipped.
"""
import asyncio
from types import SimpleNamespace

import pytest

import emas
from bot_runtime.ema200_utbot_rsi import EMA200_UTBOT_RSI_STRATEGY
from tests.test_utbreakout_sl_lockout import _build_engine

M15 = 15 * 60 * 1000
SYMBOL = "HYPE/USDT:USDT"


def _trend_rows(direction, n=120):
    rows = []
    price = 100.0
    step = 0.4 if direction == "up" else -0.4
    for i in range(n):
        close = price + step
        rows.append([
            i * M15,
            price,
            max(price, close) + 0.05,
            min(price, close) - 0.05,
            close,
            1000.0,
        ])
        price = close
    return rows


def _engine_with_rows(rows_or_error, exit_timeframe="15m"):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    params = {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"exit_timeframe": exit_timeframe},
    }
    calls = []

    def fetch_ohlcv(symbol, timeframe, limit=250):
        calls.append(timeframe)
        if isinstance(rows_or_error, Exception):
            raise rows_or_error
        return [list(row) for row in rows_or_error]

    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    engine.get_runtime_strategy_params = lambda: params
    return engine, calls


@pytest.mark.parametrize(
    "trend,side,expected",
    [
        ("up", "long", True),
        ("up", "short", False),
        ("down", "short", True),
        ("down", "long", False),
    ],
)
def test_alignment_uses_real_ut_state_on_selected_exit_timeframe(
    trend, side, expected
):
    engine, calls = _engine_with_rows(_trend_rows(trend), exit_timeframe="30m")

    aligned, reason = asyncio.run(
        engine._ema200_exit_timeframe_aligned(SYMBOL, side)
    )

    assert aligned is expected
    assert calls == ["30m"]
    assert "30m" in reason


def test_alignment_fails_closed_when_exit_candles_unavailable():
    engine, _ = _engine_with_rows(RuntimeError("network down"))

    aligned, reason = asyncio.run(
        engine._ema200_exit_timeframe_aligned(SYMBOL, "short")
    )

    assert aligned is False
    assert "확인 실패" in reason


def test_alignment_fails_closed_on_insufficient_history():
    engine, _ = _engine_with_rows(_trend_rows("down", n=10))

    aligned, reason = asyncio.run(
        engine._ema200_exit_timeframe_aligned(SYMBOL, "short")
    )

    assert aligned is False
    assert "데이터 부족" in reason


def _entry_engine(tmp_path, exit_rows):
    engine = _build_engine(tmp_path)
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"exit_timeframe": "15m"},
    }

    async def volume_allowed(_symbol):
        return True, ""

    engine._ema200_entry_volume_allowed = volume_allowed
    engine.market_data_exchange = SimpleNamespace(
        fetch_ohlcv=lambda *args, **kwargs: [list(row) for row in exit_rows]
    )
    reached = []

    def next_gate(symbol):
        reached.append(symbol)
        return True, "stop test after the exit-timeframe gate"

    engine._is_automatic_daily_symbol_entry_locked = next_gate
    return engine, reached


def test_entry_skips_short_when_exit_timeframe_ut_is_long(tmp_path):
    engine, reached = _entry_engine(tmp_path, _trend_rows("up"))

    asyncio.run(engine.entry(SYMBOL, "short", 88.98))

    assert reached == []
    reason = engine.last_entry_reason[SYMBOL]
    assert reason.startswith("EMA200_EXIT_TF_OPPOSITE")
    assert "15m" in reason
    assert any("진입 보류" in message for message in engine.ctrl.messages)


def test_entry_continues_when_exit_timeframe_ut_agrees(tmp_path):
    engine, reached = _entry_engine(tmp_path, _trend_rows("down"))

    asyncio.run(engine.entry(SYMBOL, "short", 88.98))

    assert reached == [SYMBOL]
    assert not str(engine.last_entry_reason.get(SYMBOL, "")).startswith(
        "EMA200_EXIT_TF_OPPOSITE"
    )

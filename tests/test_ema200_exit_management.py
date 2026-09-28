"""EMA200 open-position exit management through the real poll loop.

These tests drive ``SignalEngine.poll_symbol`` -> ``process_exit_candle`` ->
``_calculate_utbot_signal`` over a synthetic 15m series.  Only exchange I/O and
order execution are faked, so a failure in status/profit-stop work before the
exit check is exercised exactly as it happens live.
"""
import asyncio
import inspect
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import emas
from bot_runtime.ema200_utbot_rsi import EMA200_UTBOT_RSI_STRATEGY

M15 = 15 * 60 * 1000
H2 = 8 * M15
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H2)
SYMBOL = "HYPE/USDT:USDT"
ENTRY_IDX = 420


def _series(n=520):
    rows = []
    price = 100.0
    for i in range(n):
        # Slow downtrend, then an oscillation that produces UT BUY flips.
        drift = -0.05 if i < 400 else 0.9 * math.sin((i - 400) / 6.0)
        close = max(1.0, price + drift + 0.02 * math.sin(i))
        rows.append([
            T0 + i * M15,
            price,
            max(price, close) + 0.05,
            min(price, close) - 0.05,
            close,
            1000.0,
        ])
        price = close
    return rows


def _aggregate_2h(rows):
    buckets = {}
    for row in rows:
        key = row[0] - (row[0] % H2)
        if key not in buckets:
            buckets[key] = list(row)
            buckets[key][0] = key
        else:
            bar = buckets[key]
            bar[2] = max(bar[2], row[2])
            bar[3] = min(bar[3], row[3])
            bar[4] = row[4]
    return [buckets[key] for key in sorted(buckets)]


def _harness(*, check_status_raises=False, profit_stop_raises=False):
    rows = _series()
    clock = {"i": ENTRY_IDX}
    entry_time = datetime.fromtimestamp(
        rows[ENTRY_IDX][0] / 1000 + 60,
        tz=timezone.utc,
    ).isoformat()
    state = {"open": True, "exits": [], "notices": []}
    params = {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"exit_timeframe": "15m", "enabled": True},
    }
    engine = emas.SignalEngine.__new__(emas.SignalEngine)

    def fetch_ohlcv(symbol, timeframe, limit=5):
        visible = rows[: clock["i"] + 1]
        data = visible if timeframe == "15m" else _aggregate_2h(visible)
        return [list(row) for row in data[-limit:]]

    async def check_status(symbol, price):
        if check_status_raises:
            raise RuntimeError("simulated status failure")
        return "SHORT" if state["open"] else "NONE"

    async def profit_stop(symbol):
        if profit_stop_raises:
            raise RuntimeError("simulated profit-stop failure")

    async def process_primary_candle(symbol, k, force=False):
        engine.last_candle_success[symbol] = True

    async def exit_position(symbol, reason):
        state["open"] = False
        state["exits"].append((clock["i"], reason))

    async def fetch_position(symbol):
        if not state["open"]:
            return True, None
        return True, {"symbol": symbol, "side": "short", "contracts": 2.72}

    async def notify(text):
        state["notices"].append(text)

    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: params
    engine.get_runtime_common_settings = lambda: {}
    engine.get_runtime_trade_config = lambda: {
        "strategy_params": params,
        "common_settings": {},
    }
    engine.trading_state_store = None
    engine.db = SimpleNamespace(get_latest_open_trade=lambda symbol: (
        {"strategy": EMA200_UTBOT_RSI_STRATEGY, "entry_time": entry_time}
        if state["open"] else None
    ))
    engine.ctrl = SimpleNamespace(notify=notify)
    engine.last_entry_reason = {}
    engine.last_candle_success = {}
    engine.scanner_active_symbol = SYMBOL
    engine.check_status = check_status
    engine._ema200_apply_margin_profit_stop = profit_stop
    engine.process_primary_candle = process_primary_candle
    engine.exit_position = exit_position
    engine._fetch_server_position_checked = fetch_position
    engine._update_stateful_diag = lambda *args, **kwargs: None

    def first_fresh_buy_idx():
        signal_params = engine._get_ema200_utbot_signal_params(params)
        for i in range(ENTRY_IDX + 1, len(rows)):
            frame = pd.DataFrame(
                rows[max(0, i - 299): i + 1],
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            signal, _, _ = engine._calculate_utbot_signal(frame, signal_params)
            if signal == "long":
                return i
        raise AssertionError("fixture produced no UT BUY after entry")

    def run(until):
        for i in range(ENTRY_IDX, until):
            clock["i"] = i
            for _ in range(2):
                asyncio.run(
                    engine.poll_symbol(SYMBOL, "2h", {"strategy_params": params})
                )
            if not state["open"]:
                break

    return SimpleNamespace(
        engine=engine,
        state=state,
        run=run,
        first_fresh_buy_idx=first_fresh_buy_idx,
    )


def test_short_exits_on_first_completed_15m_ut_buy_after_entry():
    harness = _harness()
    buy_idx = harness.first_fresh_buy_idx()

    harness.run(buy_idx + 5)

    assert harness.state["exits"] == [(buy_idx, "EMA200_UTBOT_RSI_UT_BUY")]


def test_profit_stop_failure_does_not_skip_mechanical_exit():
    harness = _harness(profit_stop_raises=True)
    buy_idx = harness.first_fresh_buy_idx()

    harness.run(buy_idx + 5)

    assert harness.state["exits"] == [(buy_idx, "EMA200_UTBOT_RSI_UT_BUY")]


def test_status_failure_falls_back_to_exit_check_and_alerts_once():
    harness = _harness(check_status_raises=True)
    buy_idx = harness.first_fresh_buy_idx()

    harness.run(buy_idx + 5)

    assert harness.state["exits"] == [(buy_idx, "EMA200_UTBOT_RSI_UT_BUY")]
    # Throttled: repeated failing polls produce a single operator alert.
    assert len(harness.state["notices"]) == 1
    assert "포지션 관리 루프 오류" in harness.state["notices"][0]
    assert SYMBOL in harness.state["notices"][0]


def test_exit_fallback_does_nothing_when_exchange_is_flat():
    harness = _harness(check_status_raises=True)
    harness.state["open"] = False

    asyncio.run(
        harness.engine._poll_symbol_exit_fallback(
            SYMBOL,
            {"strategy_params": {"active_strategy": EMA200_UTBOT_RSI_STRATEGY}},
            reason="test",
        )
    )

    assert harness.state["exits"] == []
    assert harness.state["notices"] == []


@pytest.mark.parametrize("exit_timeframe", ["15m", "30m", "1h"])
def test_open_position_reason_reports_selected_exit_timeframe(exit_timeframe):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    params = {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"exit_timeframe": exit_timeframe},
    }
    engine.get_runtime_strategy_params = lambda: params
    engine.get_runtime_common_settings = lambda: {}
    engine.trading_state_store = None
    engine.db = SimpleNamespace(get_latest_open_trade=lambda symbol: None)
    engine.last_entry_reason = {}

    asyncio.run(
        engine._handle_ema200_utbot_rsi_primary_strategy(
            SYMBOL, {"c": "1"}, {"side": "short"}, "EMA200", "", None,
        )
    )

    reason = engine.last_entry_reason[SYMBOL]
    assert f"{exit_timeframe}봉" in reason
    assert "2시간봉" not in reason


def test_status_exit_timeframe_is_not_hardcoded_to_entry_timeframe():
    source = inspect.getsource(emas.SignalEngine.check_status)
    assert "symbol_status['exit_tf'] = '2h'" not in source
    assert "symbol_status['exit_tf'] = self._get_exit_timeframe(symbol)" in source

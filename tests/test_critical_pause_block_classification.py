import asyncio
import json

import emas
from trading_safety.order_state import SQLiteTradingStateStore


def test_global_pause_blocks_five_symbols_once(tmp_path, monkeypatch):
    pause_file = tmp_path / "critical_pause_state.json"
    pause_file.write_text(json.dumps({
        "status": "CRITICAL_PAUSED",
        "scope": "GLOBAL",
        "origin_symbol": "HMSTR/USDT:USDT",
        "reason_code": "LIQUIDATION_SAFETY_CONFLICT",
        "pause_id": "pause-one",
    }), encoding="utf-8")
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(pause_file))

    notifications = []
    traces = []
    store = SQLiteTradingStateStore(tmp_path / "state.db")

    class Exchange:
        id = "test"

    class Controller:
        is_paused = False
        trading_state_store = store

        async def _assert_symbol_tradeable_in_current_exchange_mode(self, symbol):
            return symbol

        async def notify(self, text):
            notifications.append(text)

    engine = object.__new__(emas.SignalEngine)
    engine.ctrl = Controller()
    engine.exchange = Exchange()
    engine.trading_state_store = store
    engine.last_entry_reason = {}
    engine.utbreakout_daily_sl_symbol_lockouts = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }
    engine.get_runtime_common_settings = lambda: {
        "live_activation_stage": "DISABLED",
        "global_trading_paused": False,
    }
    engine._is_utbreakout_daily_sl_locked = lambda symbol: (False, None)
    engine._utbreakout_trace_event = lambda *args, **kwargs: traces.append((args, kwargs))

    async def run():
        for symbol in ("AVAXUSDT", "PARTIUSDT", "XLMUSDT", "TAOUSDT", "ONDOUSDT"):
            await engine.entry(symbol, "long", 1.0)

    asyncio.run(run())

    blocked = [item for item in traces if len(item[0]) > 1 and item[0][1] == "ENTRY_BLOCKED"]
    assert len(blocked) == 5
    assert len(notifications) == 1
    assert "주문 미전송" in notifications[0]
    assert all("CRITICAL_PAUSE" in value for value in engine.last_entry_reason.values())


def test_cfg_global_pause_still_blocks_without_critical_file(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(tmp_path / "missing.json"))
    assert emas.load_critical_pause_state() is None
    try:
        emas.assert_trading_allowed(
            "BTC/USDT:USDT",
            {"global_trading_paused": True},
            include_critical_pause=False,
        )
    except RuntimeError as exc:
        assert str(exc) == "TRADING_GLOBAL_PAUSED"
    else:
        raise AssertionError("cfg global pause must block")

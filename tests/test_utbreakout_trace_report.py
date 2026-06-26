import asyncio

import emas


def _build_trace_engine():
    engine = object.__new__(emas.SignalEngine)
    engine.utbreakout_entry_trace = {}
    engine.utbreakout_last_ready_ts = {}
    engine.utbreakout_last_ready_side = {}
    engine.utbreakout_last_order_attempt_ts = {}
    engine.utbreakout_last_watchdog_report_ts = {}
    engine.utbreakout_trace_watchdog_enabled = True
    engine.utbot_filtered_breakout_entry_plans = {}
    engine.last_utbot_filtered_breakout_status = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }
    engine._get_utbot_filtered_breakout_config = lambda params: {
        "effective_profile_version": emas.UTBREAKOUT_EFFECTIVE_PROFILE_VERSION,
        "utbreakout_trace_watchdog_wait_sec": 1.0,
        "utbreakout_trace_watchdog_cooldown_sec": 180.0,
    }
    return engine


def test_utbreakout_trace_report_detects_ready_without_entry():
    engine = _build_trace_engine()

    engine._utbreakout_trace_event(
        "SOL/USDT:USDT",
        "STATUS_READY",
        "READY",
        side="long",
        qty=0.6,
    )

    report = engine._format_utbreakout_trace_report(
        "SOL/USDT:USDT",
        full=True,
    )

    assert "UTBreakout Entry Trace Report" in report
    assert "Effective Profile" in report
    assert "STATUS_READY" in report
    assert "ENTRY_CALL" in report
    assert "scanner_seen 여부: False" in report
    assert "order_attempt 여부: False" in report
    assert "suspected_break_stage" in report
    assert "STATUS_READY 이후 AUTO_ENTRY_BRIDGE/ENTRY_CALL 전" in report


def test_utbreakout_trace_is_bounded_and_symbol_aliases_share_state():
    engine = _build_trace_engine()

    for index in range(205):
        engine._utbreakout_trace_event(
            "SOL/USDT:USDT",
            "POLL_TICK",
            "SEEN",
            index=index,
        )

    events = engine._utbreakout_recent_trace_events("SOLUSDT", limit=300)

    assert len(events) == 200
    assert events[0]["data"]["index"] == 5
    assert events[-1]["data"]["index"] == 204


def test_utbreakout_watchdog_records_without_sending_telegram_after_ready_without_order():
    notifications = []

    class Controller:
        async def notify(self, text):
            notifications.append(text)

    engine = _build_trace_engine()
    engine.ctrl = Controller()
    symbol = "SOL/USDT:USDT"
    engine._utbreakout_trace_event(
        symbol,
        "STATUS_READY",
        "READY",
        side="long",
    )
    key = engine._utbreakout_trace_key(symbol)
    engine.utbreakout_last_ready_ts[key] -= 2.0

    sent = asyncio.run(
        engine._utbreakout_entry_watchdog_check(
            symbol,
            ready_side="long",
            source="test",
        )
    )

    assert sent is True
    assert notifications == []
    events = engine._utbreakout_recent_trace_events(symbol, limit=10)
    assert events[-1]["stage"] == "WATCHDOG"
    assert events[-1]["status"] == "READY_BUT_NO_ORDER_ATTEMPT"


def test_utbreakout_watchdog_does_not_report_after_order_attempt():
    notifications = []

    class Controller:
        async def notify(self, text):
            notifications.append(text)

    engine = _build_trace_engine()
    engine.ctrl = Controller()
    symbol = "SOL/USDT:USDT"
    engine._utbreakout_trace_event(
        symbol,
        "STATUS_READY",
        "READY",
        side="long",
    )
    key = engine._utbreakout_trace_key(symbol)
    engine.utbreakout_last_ready_ts[key] -= 2.0
    engine._utbreakout_trace_event(
        symbol,
        "ORDER_ATTEMPT",
        "SENT",
        side="long",
    )

    sent = asyncio.run(
        engine._utbreakout_entry_watchdog_check(
            symbol,
            ready_side="long",
            source="test",
        )
    )

    assert sent is False
    assert notifications == []

import asyncio

import emas


def _build_engine():
    engine = object.__new__(emas.SignalEngine)
    engine.utbreakout_entry_trace = {}
    engine.utbreakout_last_ready_ts = {}
    engine.utbreakout_last_ready_side = {}
    engine.utbreakout_last_order_attempt_ts = {}
    engine.utbreakout_last_watchdog_report_ts = {}
    engine.utbreakout_trace_watchdog_enabled = True
    engine.utbot_filtered_breakout_entry_plans = {}
    engine.last_utbot_filtered_breakout_status = {}
    return engine


def test_trace_symbol_resolution_prefers_last_status_symbol():
    engine = _build_engine()
    engine.utbreakout_last_status_symbol = "SOL/USDT:USDT"
    engine.utbreakout_last_ready_symbol = "ETH/USDT:USDT"
    engine.current_utbreakout_candidate_symbol = "BTC/USDT:USDT"
    engine.current_coin_selector_symbol = "SOXL/USDT:USDT"
    engine.symbol = "XRP/USDT"

    assert engine._resolve_utbreakout_trace_symbol(None) == "SOL/USDT:USDT"
    assert engine._resolve_utbreakout_trace_symbol("ADA/USDT") == "ADA/USDT"
    assert engine._resolve_utbreakout_trace_symbol("all") is None


def test_trace_report_shows_available_keys_when_no_events_for_symbol():
    engine = _build_engine()
    engine._utbreakout_trace_event(
        "SOL/USDT:USDT",
        "STATUS_READY",
        "READY",
        side="long",
    )

    report = engine._format_utbreakout_trace_report(
        "SOXL/USDT:USDT",
        full=True,
    )

    assert "available trace keys" in report
    assert "SOLUSDT" in report
    assert "symbol_key: SOXLUSDT" in report
    assert "no trace events" in report
    assert "기본 심볼 선택" in report


def test_trace_report_has_status_evaluated_and_ready_events():
    engine = _build_engine()
    engine._utbreakout_trace_event(
        "SOL/USDT:USDT",
        "status_evaluated",
        "OK",
    )
    engine._utbreakout_trace_event(
        "SOL/USDT:USDT",
        "status_ready",
        "READY",
        side="long",
    )

    report = engine._format_utbreakout_trace_report(
        "SOL/USDT:USDT",
        full=True,
    )

    assert "STATUS_EVALUATED" in report
    assert "STATUS_READY" in report
    assert "READY" in report
    assert engine.utbreakout_last_status_symbol == "SOL/USDT:USDT"
    assert engine.utbreakout_last_ready_symbol == "SOL/USDT:USDT"


def test_trace_long_text_prefers_plain_telegram_sender():
    sent = []

    class Controller:
        async def notify_plain(self, text):
            sent.append(("plain", text))

        async def notify(self, text):
            sent.append(("markdown", text))

    engine = _build_engine()
    engine.ctrl = Controller()

    asyncio.run(engine._notify_long_text("STATUS_READY / accepted_side"))

    assert sent == [("plain", "STATUS_READY / accepted_side")]


def test_status_builder_records_status_evaluated_even_in_unsupported_mode():
    engine = _build_engine()
    engine.get_runtime_strategy_params = lambda: {}
    engine._get_utbot_filtered_breakout_config = lambda params: {
        "effective_profile_version": emas.UTBREAKOUT_EFFECTIVE_PROFILE_VERSION,
        "entry_timeframe": "15m",
        "htf_timeframe": "1h",
    }
    engine.get_runtime_common_settings = lambda: {"leverage": 5}
    engine.is_upbit_mode = lambda: True

    asyncio.run(
        engine.build_utbreakout_condition_status_text("SOL/USDT:USDT")
    )

    events = engine._utbreakout_recent_trace_events(
        "SOL/USDT:USDT",
        limit=10,
    )
    assert events[-1]["stage"] == "STATUS_EVALUATED"
    assert events[-1]["status"] == "UNSUPPORTED_MODE"
    assert engine.utbreakout_last_status_symbol == "SOL/USDT:USDT"

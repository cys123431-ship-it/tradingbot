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
    return engine


def test_canonical_futures_symbol_normalizes_common_forms():
    engine = _build_engine()

    assert engine._canonical_futures_symbol("SOLUSDT") == "SOL/USDT:USDT"
    assert engine._canonical_futures_symbol("SOL/USDT") == "SOL/USDT:USDT"
    assert engine._canonical_futures_symbol("SOL/USDT:USDT") == "SOL/USDT:USDT"
    assert engine._canonical_futures_symbol("SOLUSDT/USDT") == "SOL/USDT:USDT"
    assert engine._canonical_futures_symbol("SOLUSDT/USDT:USDT") == "SOL/USDT:USDT"
    assert engine._canonical_futures_symbol("BTCUSDT") == "BTC/USDT:USDT"


def test_allo_and_al_are_not_confused():
    engine = _build_engine()

    assert (
        engine._canonical_futures_symbol("ALLOUSDT")
        == "ALLO/USDT:USDT"
    )
    assert engine._canonical_futures_symbol("ALUSDT") == "AL/USDT:USDT"
    assert engine._utbreakout_trace_key("ALLOUSDT") == "ALLOUSDT"
    assert engine._utbreakout_trace_key("ALUSDT") == "ALUSDT"


def test_utbreakout_trace_key_does_not_duplicate_quote():
    engine = _build_engine()

    for symbol in (
        "SOLUSDT",
        "SOL/USDT",
        "SOL/USDT:USDT",
        "SOLUSDT/USDT",
        "SOLUSDT/USDT:USDT",
    ):
        assert engine._utbreakout_trace_key(symbol) == "SOLUSDT"

    assert engine._utbreakout_trace_key("SOLUSDT/USDT") != "SOLUSDTUSDT"


def test_resolve_trace_symbol_prefers_canonical_last_status_symbol():
    engine = _build_engine()
    engine.utbreakout_last_status_symbol = "SOLUSDT/USDT"
    engine.utbreakout_last_ready_symbol = None
    engine.current_utbreakout_candidate_symbol = "AXS/USDT:USDT"
    engine.current_coin_selector_symbol = None
    engine.symbol = None

    assert engine._resolve_utbreakout_trace_symbol(None) == "SOL/USDT:USDT"
    assert engine._resolve_utbreakout_trace_symbol("SOLUSDT") == "SOL/USDT:USDT"


def test_trace_events_for_bad_alias_are_stored_under_canonical_key():
    engine = _build_engine()

    engine._utbreakout_trace_event(
        "SOLUSDT/USDT",
        "STATUS_EVALUATED",
        "OK",
    )

    assert list(engine.utbreakout_entry_trace) == ["SOLUSDT"]
    event = engine.utbreakout_entry_trace["SOLUSDT"][-1]
    assert event["symbol"] == "SOL/USDT:USDT"
    assert engine.utbreakout_last_status_symbol == "SOL/USDT:USDT"


def test_trace_report_normalizes_bad_input_symbol_and_key():
    engine = _build_engine()
    engine.get_runtime_strategy_params = lambda: {}
    engine._get_utbot_filtered_breakout_config = lambda params: {}
    engine.last_utbot_filtered_breakout_status = {}
    engine._utbreakout_trace_event(
        "SOL/USDT:USDT",
        "STATUS_EVALUATED",
        "OK",
    )

    report = engine._format_utbreakout_trace_report(
        "SOLUSDT/USDT",
        full=True,
    )

    assert "canonical symbol: SOL/USDT:USDT" in report
    assert "symbol_key: SOLUSDT" in report
    assert "SOLUSDTUSDT" not in report


def test_entry_plan_aliases_share_canonical_plan_symbol():
    engine = _build_engine()

    engine._set_utbot_filtered_breakout_entry_plan(
        "SOLUSDT/USDT",
        {"side": "long", "qty": 0.25, "entry_price": 100.0},
    )

    for symbol in (
        "SOLUSDT",
        "SOL/USDT",
        "SOL/USDT:USDT",
        "SOLUSDT/USDT",
    ):
        plan = engine._get_utbot_filtered_breakout_entry_plan(symbol, "long")
        assert plan["plan_symbol"] == "SOL/USDT:USDT"


def test_status_fetch_uses_canonical_futures_symbol():
    requested = []

    class MarketDataExchange:
        def fetch_ohlcv(self, symbol, timeframe, limit=300):
            requested.append(symbol)
            raise RuntimeError("stop after symbol assertion")

    engine = _build_engine()
    engine.market_data_exchange = MarketDataExchange()
    engine.get_runtime_strategy_params = lambda: {}
    engine._get_utbot_filtered_breakout_config = lambda params: {
        "effective_profile_version": emas.UTBREAKOUT_EFFECTIVE_PROFILE_VERSION,
        "entry_timeframe": "15m",
        "htf_timeframe": "1h",
    }
    engine.get_runtime_common_settings = lambda: {"leverage": 5}
    engine.is_upbit_mode = lambda: False

    asyncio.run(
        engine.build_utbreakout_condition_status_text("SOLUSDT/USDT")
    )

    assert requested == ["SOL/USDT:USDT"]
    assert engine.utbreakout_last_status_symbol == "SOL/USDT:USDT"
    assert list(engine.utbreakout_entry_trace) == ["SOLUSDT"]

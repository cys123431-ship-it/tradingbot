import asyncio

import emas


class DummyExchange:
    def __init__(self):
        self.markets = {
            "SOL/USDT:USDT": {},
            "ALLO/USDT:USDT": {},
            "BTC/USDT:USDT": {},
        }


def _build_engine():
    engine = object.__new__(emas.SignalEngine)
    engine.exchange = DummyExchange()
    engine.market_data_exchange = DummyExchange()
    engine.utbreakout_entry_trace = {}
    engine.utbreakout_last_ready_ts = {}
    engine.utbreakout_last_ready_side = {}
    engine.utbreakout_last_order_attempt_ts = {}
    engine.utbreakout_last_watchdog_report_ts = {}
    engine.utbreakout_trace_watchdog_enabled = True
    engine.utbot_filtered_breakout_entry_plans = {}
    engine.last_utbot_filtered_breakout_status = {}
    engine.last_entry_reason = {}
    return engine


def test_invalid_market_filter_rejects_missing_symbol():
    engine = _build_engine()

    ok, canonical, reason = engine._ensure_valid_utbreakout_market_symbol(
        "ALUSDT",
        source="test",
    )

    assert ok is False
    assert canonical == "AL/USDT:USDT"
    assert "REJECTED_INVALID_MARKET_SYMBOL" in reason


def test_valid_market_filter_accepts_existing_symbol():
    engine = _build_engine()

    ok, canonical, reason = engine._ensure_valid_utbreakout_market_symbol(
        "SOLUSDT",
        source="test",
    )

    assert ok is True
    assert canonical == "SOL/USDT:USDT"
    assert reason == ""


def test_allo_does_not_get_shortened_to_al():
    engine = _build_engine()

    assert (
        engine._canonical_futures_symbol("ALLOUSDT")
        == "ALLO/USDT:USDT"
    )

    ok, canonical, reason = engine._ensure_valid_utbreakout_market_symbol(
        "ALLOUSDT",
        source="test",
    )

    assert ok is True
    assert canonical == "ALLO/USDT:USDT"
    assert reason == ""


def test_invalid_market_records_trace_event():
    engine = _build_engine()
    engine.get_runtime_strategy_params = lambda: {}
    engine._get_utbot_filtered_breakout_config = lambda params: {}

    ok, canonical, _ = engine._ensure_valid_utbreakout_market_symbol(
        "ALUSDT",
        source="test",
    )

    report = engine._format_utbreakout_trace_report("ALUSDT", full=True)

    assert ok is False
    assert canonical == "AL/USDT:USDT"
    assert "SYMBOL_INVALID_MARKET" in report
    assert "AL/USDT:USDT" in report
    assert "존재하지 않는 Binance Futures 심볼 차단" in report


def test_invalid_market_status_stops_before_market_data_fetch():
    engine = _build_engine()

    def fetch_ohlcv(*args, **kwargs):
        raise AssertionError("invalid status must not fetch market data")

    engine.market_data_exchange.fetch_ohlcv = fetch_ohlcv
    engine.get_runtime_strategy_params = lambda: {}
    engine._get_utbot_filtered_breakout_config = lambda params: {
        "effective_profile_version": (
            emas.UTBREAKOUT_EFFECTIVE_PROFILE_VERSION
        ),
        "entry_timeframe": "15m",
        "htf_timeframe": "1h",
    }
    engine.get_runtime_common_settings = lambda: {"leverage": 5}
    engine.is_upbit_mode = lambda: False

    text = asyncio.run(
        engine.build_utbreakout_condition_status_text("ALUSDT")
    )

    assert "최종: 진입 차단" in text
    assert "유효하지 않은 Binance Futures 심볼" in text
    events = engine._utbreakout_recent_trace_events(
        "AL/USDT:USDT",
        limit=20,
    )
    assert any(
        event["stage"] == "SYMBOL_INVALID_MARKET"
        for event in events
    )
    assert not any(event["stage"] == "STATUS_READY" for event in events)


def test_invalid_market_entry_stops_before_exchange_preflight():
    notifications = []
    preflight_calls = []

    class Controller:
        async def notify(self, text):
            notifications.append(text)

        async def _assert_symbol_tradeable_in_current_exchange_mode(
            self,
            symbol,
        ):
            preflight_calls.append(symbol)
            raise AssertionError("invalid entry must stop before preflight")

    engine = _build_engine()
    engine.ctrl = Controller()
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }

    asyncio.run(engine.entry("ALUSDT", "long", 100.0))

    assert preflight_calls == []
    assert any(
        "유효하지 않은 Binance Futures 심볼" in text
        for text in notifications
    )
    events = engine._utbreakout_recent_trace_events(
        "AL/USDT:USDT",
        limit=20,
    )
    assert any(event["stage"] == "ENTRY_BLOCKED" for event in events)
    assert not any(event["stage"] == "ORDER_ATTEMPT" for event in events)


def test_invalid_market_bridge_stops_before_entry_call():
    entry_calls = []
    engine = _build_engine()
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }

    async def entry(*args):
        entry_calls.append(args)

    engine.entry = entry

    called = asyncio.run(
        engine._maybe_run_utbreakout_auto_entry_bridge(
            "ALUSDT",
            source="scanner_seen",
        )
    )

    assert called is False
    assert entry_calls == []
    events = engine._utbreakout_recent_trace_events(
        "AL/USDT:USDT",
        limit=20,
    )
    assert any(
        event["stage"] == "AUTO_ENTRY_BRIDGE_BLOCKED"
        and event["status"] == "INVALID_MARKET"
        for event in events
    )
    assert not any(event["stage"] == "ENTRY_CALL" for event in events)


def test_invalid_selected_candidate_never_reaches_coin_selected():
    engine = _build_engine()
    engine.coin_selector_candidate_cooldowns = {}
    engine._get_coin_selector_config = lambda: {
        "top_n": 10,
        "candidate_cooldown_enabled": False,
    }
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }
    engine._micro_auto_enabled = lambda: False

    async def evaluate_coin_selector(force=False):
        return {
            "selected": [{
                "exchange_symbol": "AL/USDT:USDT",
                "normalized_symbol": "AL/USDT",
                "selection_state": "SELECTED",
                "score": 99.0,
            }]
        }

    engine.evaluate_coin_selector = evaluate_coin_selector

    asyncio.run(engine._scan_and_trade_coin_selector())

    events = engine._utbreakout_recent_trace_events(
        "AL/USDT:USDT",
        limit=20,
    )
    assert any(
        event["stage"] == "SYMBOL_INVALID_MARKET"
        for event in events
    )
    assert not any(event["stage"] == "COIN_SELECTED" for event in events)

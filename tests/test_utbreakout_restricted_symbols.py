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
    engine.last_entry_reason = {}
    return engine


def test_restricted_symbol_base_detection():
    engine = _build_engine()

    for symbol in (
        "SKHYNIX",
        "SKHYNIXUSDT",
        "SKHYNIX/USDT:USDT",
        "HYUNDAI",
        "HYUNDAIUSDT",
        "HYUNDAI/USDT:USDT",
        "SAMSUNG",
        "SAMSUNGUSDT",
        "SAMSUNG/USDT:USDT",
    ):
        assert engine._is_utbreakout_restricted_symbol(symbol)

    for symbol in (
        "SKHY",
        "SKHYUSDT",
        "SKHY/USDT:USDT",
    ):
        assert not engine._is_utbreakout_restricted_symbol(symbol)

    assert not engine._is_utbreakout_restricted_symbol("SOLUSDT")
    assert not engine._is_utbreakout_restricted_symbol("BTC/USDT:USDT")
    assert not engine._is_utbreakout_restricted_symbol("ETHUSDT")


def test_restricted_status_is_blocked_before_market_data_fetch():
    class MarketDataExchange:
        def fetch_ohlcv(self, *args, **kwargs):
            raise AssertionError("restricted status must not fetch market data")

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

    text = asyncio.run(
        engine.build_utbreakout_condition_status_text("SAMSUNGUSDT")
    )

    assert "최종: 진입 차단" in text
    assert "한국계정 거래 제한" in text
    stages = [
        event["stage"]
        for event in engine._utbreakout_recent_trace_events(
            "SAMSUNG/USDT:USDT",
            limit=20,
        )
    ]
    assert "SYMBOL_BLOCKED_REGION_RESTRICTED" in stages
    assert "STATUS_READY" not in stages


def test_restricted_entry_returns_before_exchange_preflight():
    notifications = []
    preflight_calls = []

    class Controller:
        async def notify(self, text):
            notifications.append(text)

        async def _assert_symbol_tradeable_in_current_exchange_mode(self, symbol):
            preflight_calls.append(symbol)
            raise AssertionError("restricted entry must stop before preflight")

    engine = _build_engine()
    engine.ctrl = Controller()
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }

    asyncio.run(engine.entry("SKHYNIXUSDT", "long", 100.0))

    assert preflight_calls == []
    assert any("한국계정 제한 티커" in text for text in notifications)
    events = engine._utbreakout_recent_trace_events(
        "SKHYNIX/USDT:USDT",
        limit=20,
    )
    assert any(
        event["stage"] == "SYMBOL_BLOCKED_REGION_RESTRICTED"
        for event in events
    )
    assert any(event["stage"] == "ENTRY_BLOCKED" for event in events)
    assert not any(event["stage"] == "ORDER_ATTEMPT" for event in events)


def test_restricted_selected_candidate_never_reaches_coin_selected():
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
                "exchange_symbol": "HYUNDAI/USDT:USDT",
                "normalized_symbol": "HYUNDAI/USDT",
                "selection_state": "SELECTED",
                "score": 99.0,
            }]
        }

    engine.evaluate_coin_selector = evaluate_coin_selector

    asyncio.run(engine._scan_and_trade_coin_selector())

    events = engine._utbreakout_recent_trace_events(
        "HYUNDAI/USDT:USDT",
        limit=20,
    )
    assert any(
        event["stage"] == "SYMBOL_BLOCKED_REGION_RESTRICTED"
        for event in events
    )
    assert not any(event["stage"] == "COIN_SELECTED" for event in events)

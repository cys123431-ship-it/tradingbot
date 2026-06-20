import asyncio

import emas


def test_utbreakout_entry_plan_symbol_alias_lookup():
    engine = object.__new__(emas.SignalEngine)
    engine.utbot_filtered_breakout_entry_plans = {}

    plan = {
        "side": "long",
        "entry_price": 71.74,
        "qty": 0.295,
    }

    engine._set_utbot_filtered_breakout_entry_plan("SOL/USDT:USDT", plan)

    assert engine._get_utbot_filtered_breakout_entry_plan("SOL/USDT:USDT", "long") is not None
    assert engine._get_utbot_filtered_breakout_entry_plan("SOL/USDT", "long") is not None
    assert engine._get_utbot_filtered_breakout_entry_plan("SOLUSDT", "long") is not None
    assert engine._get_utbot_filtered_breakout_entry_plan("SOL/USDT", "short") is None


def test_utbreakout_entry_plan_clear_removes_all_aliases():
    engine = object.__new__(emas.SignalEngine)
    engine.utbot_filtered_breakout_entry_plans = {}
    engine._set_utbot_filtered_breakout_entry_plan(
        "SOL/USDT:USDT",
        {"side": "long", "entry_price": 71.74, "qty": 0.295},
    )

    engine._clear_utbot_filtered_breakout_entry_plan("SOLUSDT")

    assert engine._get_utbot_filtered_breakout_entry_plan("SOL/USDT:USDT", "long") is None
    assert engine._get_utbot_filtered_breakout_entry_plan("SOL/USDT", "long") is None
    assert engine.utbot_filtered_breakout_entry_plans == {}


def test_utbreakout_poll_retries_processed_candle_when_no_position():
    symbol = "SOL/USDT:USDT"
    closed_ts = 1_718_800_000_000

    class MarketDataExchange:
        def fetch_ohlcv(self, requested_symbol, timeframe, limit=5):
            assert requested_symbol == symbol
            assert timeframe == "15m"
            return [
                [closed_ts - 900_000, 70.0, 71.0, 69.0, 70.5, 1000.0],
                [closed_ts, 70.5, 72.0, 70.0, 71.5, 1200.0],
                [closed_ts + 900_000, 71.5, 72.5, 71.0, 72.0, 800.0],
            ]

    engine = object.__new__(emas.SignalEngine)
    engine.market_data_exchange = MarketDataExchange()
    engine.last_processed_candle_ts = {symbol: closed_ts}
    engine.last_state_sync_candle_ts = {symbol: closed_ts}
    engine.last_stateful_retry_ts = {symbol: 0.0}
    engine.last_processed_exit_candle_ts = {}
    engine.last_candle_time = {}
    engine.utbreakout_adaptive_tf_state = {}
    engine.utbreakout_adaptive_last_decision_ts = {}
    engine.utbreakout_last_selected_set_ids = {}
    engine.fisher_states = {}
    engine.vbo_states = {}
    engine.cameron_states = {}
    engine._get_primary_poll_timeframe = lambda requested_symbol, cfg: "15m"
    engine.is_upbit_mode = lambda: False

    calls = []

    async def check_status(requested_symbol, current_price):
        return "NONE"

    async def process_primary_candle(requested_symbol, candle, force=False):
        calls.append((requested_symbol, candle["t"], force))

    engine.check_status = check_status
    engine.process_primary_candle = process_primary_candle

    asyncio.run(
        engine.poll_symbol(
            symbol,
            "15m",
            {
                "strategy_params": {
                    "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
                    "entry_mode": "cross",
                }
            },
        )
    )

    assert calls == [(symbol, closed_ts, True)]


def test_utbreakout_entry_reaches_market_order_and_reports_exchange_failure(monkeypatch):
    raw_symbol = "SOL/USDT:USDT"
    order_symbol = "SOL/USDT"
    notifications = []
    order_calls = []
    diagnostics = []

    class Controller:
        async def _assert_symbol_tradeable_in_current_exchange_mode(self, symbol):
            assert symbol == raw_symbol
            return order_symbol

        async def notify(self, text):
            notifications.append(text)

    class Exchange:
        id = "test"

        def fetch_positions(self):
            return []

        def load_markets(self):
            return {
                order_symbol: {
                    "limits": {"cost": {"min": 5.0}},
                    "info": {},
                }
            }

        def amount_to_precision(self, symbol, amount):
            assert symbol == order_symbol
            return f"{amount:.3f}"

        def create_order(self, symbol, order_type, side, qty):
            order_calls.append((symbol, order_type, side, qty))
            raise RuntimeError("exchange rejected test order")

    engine = object.__new__(emas.SignalEngine)
    engine.ctrl = Controller()
    engine.exchange = Exchange()
    engine.last_entry_reason = {}
    engine.utbot_filtered_breakout_entry_plans = {}
    engine.get_runtime_common_settings = lambda: {
        "leverage": 5,
        "risk_per_trade_pct": 1.0,
        "live_activation_stage": "DISABLED",
    }
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }
    engine.is_trade_direction_allowed = lambda side: True
    engine.is_upbit_mode = lambda: False
    engine._get_utbot_filtered_breakout_config = lambda strategy_params=None: {
        "entry_timeframe": "15m",
        "htf_timeframe": "1h",
    }
    engine._record_utbreakout_diagnostic_event = (
        lambda symbol, status, event=None, extra=None: diagnostics.append(
            (symbol, event, dict(extra or {}))
        )
    )

    async def get_balance_info():
        return 99.0, 99.0, 0.0

    async def ensure_market_settings(symbol, leverage):
        assert symbol == order_symbol
        assert leverage == 5

    engine.get_balance_info = get_balance_info
    engine.ensure_market_settings = ensure_market_settings
    engine._set_utbot_filtered_breakout_entry_plan(
        raw_symbol,
        {
            "side": "long",
            "entry_price": 71.74,
            "qty": 0.295,
            "risk_usdt": 0.54,
            "risk_distance": 1.83,
        },
    )
    monkeypatch.setattr(emas, "assert_trading_allowed", lambda symbol, cfg=None: True)

    asyncio.run(engine.entry(raw_symbol, "long", 71.74))

    assert order_calls == [(order_symbol, "market", "buy", "0.295")]
    assert any(text.startswith("🟡 UTBreakout 주문 시도:") for text in notifications)
    assert any(text.startswith("❌ UTBreakout 주문 실패:") for text in notifications)
    assert engine.last_entry_reason[order_symbol].startswith("ORDER_FAILED: RuntimeError:")
    assert engine.last_entry_reason[raw_symbol] == engine.last_entry_reason[order_symbol]
    assert diagnostics[-1][1] == "entry_blocked"
    assert diagnostics[-1][2]["code"] == "ENTRY_ORDER_FAILED"

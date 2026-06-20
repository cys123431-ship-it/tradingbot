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
    engine.last_utbreakout_no_position_retry_ts = {}
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
                    "UTBotFilteredBreakoutV1": {
                        "utbreakout_no_position_retry_interval_sec": 7.0,
                    },
                }
            },
        )
    )

    assert calls == [(symbol, closed_ts, True)]
    assert f"{symbol}:15m:{closed_ts}" in engine.last_utbreakout_no_position_retry_ts


def test_utbreakout_force_reprocess_bypasses_adaptive_duplicate_guard():
    engine = object.__new__(emas.SignalEngine)
    engine.utbreakout_adaptive_last_decision_ts = {
        "SOL/USDT:USDT": {"15m": 1_718_800_000_000}
    }

    skipped, last_ts = engine._utbreakout_should_skip_adaptive_decision(
        "SOL/USDT:USDT",
        "15m",
        1_718_800_000_000,
    )
    forced_skip, forced_last_ts = engine._utbreakout_should_skip_adaptive_decision(
        "SOL/USDT:USDT",
        "15m",
        1_718_800_000_000,
        force_reprocess=True,
    )

    assert skipped is True
    assert last_ts == 1_718_800_000_000
    assert forced_skip is False
    assert forced_last_ts == 1_718_800_000_000


def test_utbreakout_strategy_signal_propagates_force_reprocess():
    engine = object.__new__(emas.SignalEngine)
    engine.last_entry_filter_status = {}
    engine.last_entry_reason = {}
    engine.vbo_states = {}
    engine.fisher_states = {}
    engine.cameron_states = {}
    engine.kalman_states = {}
    calls = []

    async def calculate(symbol, df, strategy_params, *, force_reprocess=False):
        calls.append(force_reprocess)
        return "long", "accepted", {}

    engine._calculate_utbot_filtered_breakout_signal = calculate

    result = asyncio.run(
        engine._calculate_strategy_signal(
            "SOL/USDT:USDT",
            None,
            {"active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY},
            emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
            force_utbreakout_reprocess=True,
        )
    )

    assert calls == [True]
    assert result[0] == "long"


def test_utbreakout_diagnostic_recovery_requires_current_plan_and_candle():
    engine = object.__new__(emas.SignalEngine)
    engine.last_utbot_filtered_breakout_status = {
        "SOL/USDT:USDT": {
            "accepted_side": "long",
            "decision_candle_ts": 1_718_800_000_000,
        }
    }
    engine.utbot_filtered_breakout_entry_plans = {}
    engine.is_trade_direction_allowed = lambda side: side == "long"
    engine._set_utbot_filtered_breakout_entry_plan(
        "SOL/USDT:USDT",
        {"side": "long", "entry_price": 71.74, "qty": 0.295},
    )

    assert engine._recover_utbreakout_accepted_side(
        "SOL/USDT",
        1_718_800_000_000,
    ) == "long"
    assert engine._recover_utbreakout_accepted_side(
        "SOL/USDT",
        1_718_800_900_000,
    ) is None


def test_force_utbreakout_entry_revalidates_and_uses_normal_entry_path():
    symbol = "SOL/USDT:USDT"
    notifications = []
    entry_calls = []
    position_state = {"position": None}

    class Controller:
        async def notify(self, text):
            notifications.append(text)

    class MarketDataExchange:
        def fetch_ohlcv(self, requested_symbol, timeframe, limit=300):
            assert requested_symbol == symbol
            assert timeframe == "15m"
            return [
                [index * 900_000, 70.0, 72.0, 69.0, 71.0, 1000.0]
                for index in range(300)
            ]

    engine = object.__new__(emas.SignalEngine)
    engine.ctrl = Controller()
    engine.market_data_exchange = MarketDataExchange()
    engine.last_entry_reason = {}
    engine.utbot_filtered_breakout_entry_plans = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }
    engine.get_runtime_trade_config = lambda: {
        "strategy_params": engine.get_runtime_strategy_params(),
    }
    engine._get_primary_poll_timeframe = lambda requested_symbol, cfg: "15m"
    engine.is_trade_direction_allowed = lambda side: True

    async def get_server_position(requested_symbol, use_cache=False):
        return position_state["position"]

    async def calculate(requested_symbol, df, strategy_params, *, force_reprocess=False):
        assert force_reprocess is True
        engine._set_utbot_filtered_breakout_entry_plan(
            requested_symbol,
            {"side": "long", "entry_price": 71.25, "qty": 0.295},
        )
        return "long", "accepted", {"accepted_side": "long"}

    async def entry(requested_symbol, side, price):
        entry_calls.append((requested_symbol, side, price))
        position_state["position"] = {"side": side, "contracts": 0.295}

    engine.get_server_position = get_server_position
    engine._calculate_utbot_filtered_breakout_signal = calculate
    engine.entry = entry

    result = asyncio.run(engine.force_utbreakout_entry_from_status(symbol))

    assert entry_calls == [(symbol, "long", 71.25)]
    assert result["status"] == "POSITION_OPENED"
    assert any(text.startswith("🟡 UTBreakout forceentry 시도:") for text in notifications)


def test_utbreakout_entry_reaches_market_order_and_reports_exchange_failure(monkeypatch):
    raw_symbol = "SOL/USDT:USDT"
    preflight_symbol = "SOL/USDT"
    order_symbol = "SOL/USDT:USDT"
    notifications = []
    order_calls = []
    diagnostics = []

    class Controller:
        async def _assert_symbol_tradeable_in_current_exchange_mode(self, symbol):
            assert symbol == raw_symbol
            return preflight_symbol

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

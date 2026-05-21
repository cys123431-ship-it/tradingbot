from datetime import datetime, timezone
import asyncio

import pytest

from utbreakout.indicators import previous_donchian
from utbreakout.research import summarize_diagnostic_events
from utbreakout.risk import calculate_risk_plan


def _signal_engine_cls():
    return pytest.importorskip("emas", reason="emas runtime dependencies are optional in CI").SignalEngine


def _emas_module():
    return pytest.importorskip("emas", reason="emas runtime dependencies are optional in CI")


def test_previous_donchian_excludes_current_candle():
    highs = [10, 11, 12, 13, 14, 999]
    lows = [9, 8, 7, 6, 5, -999]

    result = previous_donchian(highs, lows, 5)

    assert result["ready"] is True
    assert result["high"] == 14
    assert result["low"] == 5


def test_risk_plan_uses_loss_budget_not_fixed_margin():
    plan = calculate_risk_plan(
        side="long",
        entry_price=100.0,
        atr_value=2.0,
        stop_atr_multiplier=1.5,
        ut_stop=96.0,
        take_profit_r_multiple=2.0,
        min_risk_reward=2.0,
        balance_usdt=4000.0,
        risk_per_trade_percent=1.0,
        max_risk_per_trade_usdt=50.0,
        leverage=10.0,
    )

    assert plan["risk_distance"] == 4.0
    assert plan["risk_usdt"] == 40.0
    assert plan["qty"] == 10.0
    assert plan["planned_notional"] == 1000.0
    assert plan["planned_margin"] == 100.0
    assert plan["take_profit"] == 108.0


def test_research_summary_detects_set_concentration_and_protection_gaps():
    events = []
    for idx in range(6):
        events.append({
            "ts": datetime(2026, 1, 1, 0, idx, tzinfo=timezone.utc).isoformat(),
            "event": "rejected",
            "symbol": "BTC/USDT",
            "side": "long",
            "code": "REJECTED_ADX_LOW",
            "auto_selected_set_id": 2,
            "candidate_type": "bias_state",
            "decision_candle_ts": idx,
            "risk_usdt": 1.0,
            "risk_distance": 10.0,
            "entry_price": 1000.0,
            "planned_margin": 5.0,
            "planned_notional": 50.0,
        })
    events.append({
        "ts": datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc).isoformat(),
        "event": "accepted",
        "symbol": "BTC/USDT",
        "side": "short",
        "code": "ACCEPTED_ENTRY",
        "auto_selected_set_id": 7,
        "candidate_type": "fresh_signal",
        "decision_candle_ts": 7,
    })

    summary = summarize_diagnostic_events(
        events,
        protection_status={"BTC/USDT": {"missing_sl": True, "missing_tp": False}},
    )

    assert summary["top_set"] == "Set2"
    assert summary["top_set_share_pct"] > 50.0
    assert summary["top_rejects"][0] == ("REJECTED_ADX_LOW", 6)
    assert summary["protection_missing_sl"] == ["BTC/USDT"]


def _market(**overrides):
    base = {
        "symbol": "BTC/USDT:USDT",
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "active": True,
        "type": "swap",
        "info": {"contractType": "PERPETUAL", "status": "TRADING"},
    }
    base.update(overrides)
    return base


def test_coin_selector_market_lookup_prefers_futures_over_spot():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    spot = _market(
        symbol="BTC/USDT",
        settle=None,
        swap=False,
        type="spot",
        info={"status": "TRADING"},
    )
    futures = _market(symbol="BTC/USDT:USDT")
    markets = {
        "BTC/USDT": spot,
        "BTC/USDT:USDT": futures,
    }

    assert engine._coin_selector_market_for_symbol("BTC/USDT", markets) is futures


def test_custom_coin_symbol_resolution_uses_futures_canonical_symbol():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    markets = {
        "BTC/USDT": _market(
            symbol="BTC/USDT",
            settle=None,
            swap=False,
            type="spot",
            info={"status": "TRADING"},
        ),
        "BTC/USDT:USDT": _market(symbol="BTC/USDT:USDT"),
    }

    for raw in ["BTC", "BTCUSDT", "BTC/USDT", "BTC/USDT:USDT"]:
        assert engine._coin_selector_exchange_symbol_for_custom(raw, markets) == "BTC/USDT:USDT"

    assert engine._coin_selector_market_for_symbol("BTC/USDC", markets) is None


class _MemoryConfig:
    def __init__(self):
        self.values = {}

    async def update_value(self, path, value):
        node = self.values
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value


class _TelegramConfig:
    def __init__(self, chat_id):
        self.chat_id = chat_id

    def get_chat_id(self):
        return self.chat_id


class _FakeTelegramChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeTelegramMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, *args, **kwargs):
        self.replies.append((args, kwargs))


class _FakeTelegramUpdate:
    def __init__(self, chat_id, text):
        self.effective_chat = _FakeTelegramChat(chat_id)
        self.message = _FakeTelegramMessage(text)
        self.effective_message = self.message
        self.callback_query = None


def _telegram_controller(chat_id=12345):
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    controller.cfg = _TelegramConfig(chat_id)
    controller.is_paused = False
    controller.active_engine = None
    return controller


def test_telegram_update_requires_configured_chat_id():
    controller = _telegram_controller(chat_id=12345)

    assert controller._is_authorized_telegram_update(_FakeTelegramUpdate(12345, "/status")) is True
    assert controller._is_authorized_telegram_update(_FakeTelegramUpdate(99999, "/status")) is False


def test_telegram_global_handler_rejects_unauthorized_stop_without_emergency_call():
    controller = _telegram_controller(chat_id=12345)
    called = False

    async def emergency_stop():
        nonlocal called
        called = True

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(99999, "STOP")

    result = asyncio.run(controller.global_handler(update, None))

    emas = _emas_module()
    assert result == emas.ConversationHandler.END
    assert called is False
    assert len(update.message.replies) == 1


def test_telegram_global_handler_requires_exact_emergency_text():
    controller = _telegram_controller(chat_id=12345)
    called = False

    async def emergency_stop():
        nonlocal called
        called = True

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(12345, "PLEASE STOP")

    result = asyncio.run(controller.global_handler(update, None))

    assert result is None
    assert called is False
    assert update.message.replies == []


def test_telegram_global_handler_accepts_authorized_exact_stop():
    controller = _telegram_controller(chat_id=12345)
    called = False

    async def emergency_stop():
        nonlocal called
        called = True

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(12345, "STOP")

    result = asyncio.run(controller.global_handler(update, None))

    emas = _emas_module()
    assert result == emas.ConversationHandler.END
    assert called is True
    assert len(update.message.replies) == 1


class _ResettableSignalEngine:
    def __init__(self):
        self.scanner_active_symbol = "ETH/USDT"
        self.reset_kwargs = None

    def reset_signal_runtime_state(self, **kwargs):
        self.reset_kwargs = kwargs


def test_return_signal_engine_to_utbot_turns_off_utbreakout_customcoins_and_scanner():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    controller.cfg = _MemoryConfig()
    signal_engine = _ResettableSignalEngine()
    controller.engines = {"signal": signal_engine}

    asyncio.run(controller._return_signal_engine_to_utbot())

    signal_cfg = controller.cfg.values["signal_engine"]
    strategy = signal_cfg["strategy_params"]
    breakout = strategy["UTBotFilteredBreakoutV1"]
    assert strategy["active_strategy"] == "utbot"
    assert breakout["adaptive_timeframe_enabled"] is False
    assert breakout["auto_select_enabled"] is False
    assert breakout["selection_mode"] == "manual"
    assert signal_cfg["coin_selector"]["enabled"] is False
    assert signal_cfg["coin_selector"]["custom_universe_enabled"] is False
    assert signal_cfg["common_settings"]["scanner_enabled"] is False
    assert signal_engine.scanner_active_symbol is None
    assert signal_engine.reset_kwargs == {
        "reset_entry_cache": True,
        "reset_exit_cache": True,
        "reset_stateful_strategy": True,
    }


def test_protection_order_classifies_binance_stop_market_from_orig_type():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    order = {
        "type": "market",
        "side": "sell",
        "info": {
            "type": "STOP_MARKET",
            "origType": "STOP_MARKET",
            "stopPrice": "78000",
            "reduceOnly": "true",
            "symbol": "BTCUSDT",
        },
    }

    assert signal_engine._classify_protection_order(engine, order) == "sl"
    assert signal_engine._protection_order_matches_symbol(engine, order, "BTC/USDT") is True


def test_protection_order_keeps_take_profit_separate_from_stop_loss():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    take_profit_market = {
        "type": "market",
        "side": "sell",
        "info": {
            "type": "TAKE_PROFIT_MARKET",
            "origType": "TAKE_PROFIT_MARKET",
            "stopPrice": "82000",
            "reduceOnly": "true",
        },
    }
    take_profit_limit = {"type": "limit", "side": "sell", "reduceOnly": True}

    assert signal_engine._classify_protection_order(engine, take_profit_market) == "tp"
    assert signal_engine._classify_protection_order(engine, take_profit_limit) == "tp"


def test_protection_order_classifies_bot_client_ids_even_without_reduce_only():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    take_profit = {"id": "tp", "type": "limit", "side": "buy", "clientOrderId": "utbtpBTCUSDTabc"}
    stop_loss = {"id": "sl", "type": "market", "side": "buy", "clientOrderId": "utbslBTCUSDTabc"}

    assert signal_engine._classify_protection_order(engine, take_profit) == "tp"
    assert signal_engine._classify_protection_order(engine, stop_loss) == "sl"


class _DummyCtrl:
    def format_symbol_for_display(self, symbol):
        return symbol

    async def notify(self, message):
        self.last_message = message


class _FakeExchange:
    def __init__(self, orders, symbol_scope_returns=True, positions=None):
        self.orders = list(orders)
        self.positions = list(positions or [])
        self.cancelled = []
        self.cancel_all_requests = []
        self.symbol_scope_returns = symbol_scope_returns

    def fetch_open_orders(self, symbol=None):
        if symbol and not self.symbol_scope_returns:
            return []
        return list(self.orders)

    def fetch_positions(self, symbols=None):
        return list(self.positions)

    def cancel_all_orders(self, symbol):
        self.cancel_all_requests.append(symbol)
        self.orders = []
        return []

    def cancel_order(self, order_id, symbol):
        self.cancelled.append((str(order_id), symbol))
        self.orders = [
            order for order in self.orders
            if str(order.get("id") or order.get("info", {}).get("orderId")) != str(order_id)
        ]
        return {"id": order_id}


def _protection_engine(orders, symbol_scope_returns=True, positions=None):
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.exchange = _FakeExchange(orders, symbol_scope_returns=symbol_scope_returns, positions=positions)
    engine.ctrl = _DummyCtrl()
    engine.last_protection_alert_ts = {}
    engine.last_protection_order_status = {}
    engine.last_orphan_protection_sweep_ts = 0.0
    engine.orphan_protection_candidates = {}
    engine.ORPHAN_PROTECTION_SWEEP_INTERVAL = 10.0
    engine.position_cache = {}
    engine.POSITION_CACHE_TTL = 0.0
    engine.is_upbit_mode = lambda: False
    return engine


def test_protection_audit_cancels_orphan_orders_even_when_symbol_fetch_misses_them():
    engine = _protection_engine(
        [
            {
                "id": "sl-old",
                "side": "buy",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "105",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        symbol_scope_returns=False,
    )

    status = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=None, expected_tp=False, expected_sl=False, alert=False)
    )

    assert status["status"] == "ORPHAN_CANCELLED"
    assert status["orphan_cancelled"] == 1
    assert engine.exchange.orders == []


def test_reconcile_closed_position_cancels_leftover_tp_and_sl_orders():
    engine = _protection_engine(
        [
            {
                "id": "tp-left",
                "side": "sell",
                "type": "limit",
                "clientOrderId": "utbtpBTCUSDTleft",
                "info": {"symbol": "BTCUSDT"},
            },
            {
                "id": "sl-left",
                "side": "sell",
                "type": "market",
                "clientOrderId": "utbslBTCUSDTleft",
                "info": {"origType": "STOP_MARKET", "stopPrice": "95", "symbol": "BTCUSDT"},
            },
        ],
        symbol_scope_returns=False,
    )

    async def _no_position(symbol, use_cache=False):
        return None

    engine.get_server_position = _no_position

    status = asyncio.run(
        engine._reconcile_closed_position_protection(
            "BTC/USDT",
            reason="tp/sl filled",
            alert=False,
            attempts=1,
        )
    )

    assert status["status"] == "ORPHAN_CANCELLED"
    assert status["orphan_cancelled"] == 2
    assert engine.exchange.orders == []


def test_global_orphan_sweep_cancels_leftover_stop_loss_without_tracked_symbol():
    engine = _protection_engine(
        [
            {
                "id": "sl-orphan",
                "side": "sell",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "95",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        positions=[],
    )

    status = asyncio.run(
        engine._cleanup_orphan_protection_orders(
            reason="test orphan sweep",
            alert=False,
            min_interval=0,
            confirm_delay_sec=0,
        )
    )

    assert status["status"] == "ORPHAN_CANCELLED"
    assert status["cancelled"] == 1
    assert status["symbols"]["BTC/USDT"]["cancelled"] == 1
    assert engine.exchange.orders == []


def test_global_orphan_sweep_keeps_orders_when_position_is_active():
    engine = _protection_engine(
        [
            {
                "id": "sl-active",
                "side": "sell",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "95",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        positions=[{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "0.1", "entryPrice": "100"}],
    )

    status = asyncio.run(
        engine._cleanup_orphan_protection_orders(
            reason="test active position sweep",
            alert=False,
            min_interval=0,
            confirm_delay_sec=0,
        )
    )

    assert status["status"] == "OK"
    assert status["cancelled"] == 0
    assert [order["id"] for order in engine.exchange.orders] == ["sl-active"]


def test_global_orphan_sweep_requires_confirmation_before_cancelling():
    engine = _protection_engine(
        [
            {
                "id": "sl-pending",
                "side": "sell",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "95",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        positions=[],
    )

    status = asyncio.run(
        engine._cleanup_orphan_protection_orders(
            reason="test pending sweep",
            alert=False,
            min_interval=0,
            confirm_delay_sec=60,
        )
    )

    assert status["status"] == "PENDING_CONFIRMATION"
    assert status["pending"] == 1
    assert [order["id"] for order in engine.exchange.orders] == ["sl-pending"]


def test_cancel_protection_order_tries_raw_binance_symbol_variant():
    class _RawOnlyExchange(_FakeExchange):
        def cancel_order(self, order_id, symbol):
            if symbol != "BTCUSDT":
                raise ValueError(f"wrong symbol {symbol}")
            return super().cancel_order(order_id, symbol)

    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.exchange = _RawOnlyExchange(
        [
            {
                "id": "sl-raw",
                "side": "buy",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "105",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ]
    )
    engine.ctrl = _DummyCtrl()
    engine.last_protection_alert_ts = {}
    engine.last_protection_order_status = {}
    engine.is_upbit_mode = lambda: False

    cancelled = asyncio.run(engine._cancel_protection_orders("BTC/USDT", reason="raw symbol fallback"))

    assert cancelled == 1
    assert engine.exchange.cancelled == [("sl-raw", "BTCUSDT")]
    assert engine.exchange.orders == []


def test_protection_audit_deduplicates_short_stop_loss_orders():
    orders = [
        {
            "id": "sl-old",
            "side": "buy",
            "type": "market",
            "timestamp": 1000,
            "info": {"origType": "STOP_MARKET", "stopPrice": "105", "reduceOnly": "true", "symbol": "BTCUSDT"},
        },
        {
            "id": "sl-new",
            "side": "buy",
            "type": "market",
            "timestamp": 2000,
            "info": {"origType": "STOP_MARKET", "stopPrice": "106", "reduceOnly": "true", "symbol": "BTCUSDT"},
        },
        {
            "id": "tp",
            "side": "buy",
            "type": "limit",
            "price": "90",
            "reduceOnly": True,
            "info": {"symbol": "BTCUSDT"},
        },
    ]
    engine = _protection_engine(orders)
    pos = {"side": "short", "contracts": 1, "entryPrice": 100}

    status = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=pos, expected_tp=True, expected_sl=True, alert=False)
    )

    assert status["status"] == "DUPLICATE_CANCELLED"
    assert status["duplicate_cancelled"] == 1
    remaining_ids = {order["id"] for order in engine.exchange.orders}
    assert remaining_ids == {"sl-new", "tp"}

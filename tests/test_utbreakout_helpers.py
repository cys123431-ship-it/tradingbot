from datetime import datetime, timezone
import asyncio
import re

import pandas as pd
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


def test_telegram_global_handler_accepts_main_keyboard_stop_button():
    controller = _telegram_controller(chat_id=12345)
    called = False

    async def emergency_stop():
        nonlocal called
        called = True

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(12345, "🚨 STOP")

    result = asyncio.run(controller.global_handler(update, None))

    emas = _emas_module()
    assert result == emas.ConversationHandler.END
    assert called is True
    assert len(update.message.replies) == 1
    assert re.match(emas.TELEGRAM_EMERGENCY_PATTERN, "🚨 STOP")


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
        self.created = []
        self.symbol_scope_returns = symbol_scope_returns

    def amount_to_precision(self, symbol, amount):
        return str(round(float(amount), 6))

    def price_to_precision(self, symbol, price):
        return str(round(float(price), 2))

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

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        params = dict(params or {})
        order_id = f"created-{len(self.created) + 1}"
        info = {
            "symbol": symbol.replace("/", "").replace(":USDT", ""),
            "reduceOnly": str(bool(params.get("reduceOnly", False))).lower(),
        }
        if str(order_type).lower() == "stop_market":
            info.update({
                "type": "STOP_MARKET",
                "origType": "STOP_MARKET",
                "stopPrice": params.get("stopPrice"),
            })
        order = {
            "id": order_id,
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "reduceOnly": bool(params.get("reduceOnly", False)),
            "clientOrderId": params.get("newClientOrderId"),
            "info": info,
            "params": params,
        }
        self.created.append(order)
        self.orders.append(order)
        return order


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


def test_utbreakout_defaults_enable_partial_trailing_and_short_guard():
    emas = _emas_module()

    cfg = emas.build_default_utbot_filtered_breakout_config()

    assert cfg["partial_take_profit_enabled"] is True
    assert cfg["partial_take_profit_r_multiple"] == 1.5
    assert cfg["partial_take_profit_ratio"] == 0.5
    assert cfg["atr_trailing_enabled"] is True
    assert cfg["atr_trailing_multiplier"] == 2.0
    assert cfg["atr_trailing_activation_r"] == 1.5
    assert cfg["short_conservative_enabled"] is True
    assert cfg["short_risk_multiplier"] == 0.5
    assert cfg["short_adx_threshold"] == 22.0
    assert cfg["market_quality_enabled"] is True
    assert cfg["market_quality_data_required"] is False
    assert cfg["market_quality_min_risk_multiplier"] == 0.25
    assert cfg["shadow_triple_barrier_enabled"] is True
    assert cfg["adaptive_exit_enabled"] is True
    assert cfg["volatility_targeting_enabled"] is True
    assert cfg["volatility_target_atr_pct"] == 1.0
    assert cfg["meta_labeling_enabled"] is True
    assert cfg["short_asymmetry_enabled"] is True
    assert cfg["shadow_runner_exit_enabled"] is True
    assert cfg["runner_exit_enabled"] is True
    assert cfg["runner_chandelier_enabled"] is True
    assert cfg["runner_chandelier_multiplier"] == 2.4
    assert cfg["trend_health_enabled"] is True


def test_utbreakout_short_guard_requires_htf_and_dmi_alignment():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = {
        "short_conservative_enabled": True,
        "short_adx_threshold": 22.0,
    }

    ok, reason = engine._utbreakout_short_guard_passes(
        cfg,
        {
            "htf_close": 95,
            "htf_ema_fast": 90,
            "htf_ema_slow": 100,
            "adx": 25,
            "plus_di": 12,
            "minus_di": 28,
        },
    )
    assert ok is True
    assert reason == "short guard passed"

    ok, reason = engine._utbreakout_short_guard_passes(
        cfg,
        {
            "htf_close": 105,
            "htf_ema_fast": 110,
            "htf_ema_slow": 100,
            "adx": 18,
            "plus_di": 30,
            "minus_di": 20,
        },
    )
    assert ok is False
    assert "ADX" in reason
    assert "-DI > +DI" in reason


def test_utbreakout_short_guard_status_item_matches_real_gate():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = {
        "short_conservative_enabled": True,
        "short_risk_multiplier": 0.5,
        "short_adx_threshold": 22.0,
    }

    label, state, detail = engine._build_utbreakout_short_guard_status_item(
        cfg,
        {
            "htf_close": 105,
            "htf_ema_fast": 110,
            "htf_ema_slow": 100,
            "adx": 18,
            "plus_di": 30,
            "minus_di": 20,
        },
    )

    assert label == "보수적 숏 가드"
    assert state is False
    assert "ADX" in detail
    assert "숏 리스크 x0.50" in detail


def test_utbreakout_market_quality_reduces_risk_without_blocking_on_mild_funding():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()

    result = engine._evaluate_utbreakout_market_quality(
        "long",
        cfg,
        {
            "atr_pct": 0.5,
            "funding_rate": 0.0007,
            "open_interest_delta_pct": 0.3,
            "taker_buy_sell_ratio": 1.04,
            "futures_spread_pct": 0.02,
        },
    )

    assert result["state"] == "reduced"
    assert 0 < result["risk_multiplier"] < 1
    assert result["hard_block"] is False
    assert "funding" in result["summary"]


def test_utbreakout_market_quality_blocks_extreme_adverse_funding():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()

    result = engine._evaluate_utbreakout_market_quality(
        "long",
        cfg,
        {
            "atr_pct": 0.5,
            "funding_rate": 0.002,
            "futures_spread_pct": 0.02,
        },
    )

    assert result["state"] is False
    assert result["risk_multiplier"] == 0
    assert result["hard_block"] is True
    assert result["summary"].startswith("BLOCK")


def test_utbreakout_market_quality_status_item_shows_reduced_state():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()

    label, state, detail = engine._build_utbreakout_market_quality_status_item(
        "short",
        cfg,
        {
            "atr_pct": 0.6,
            "taker_buy_sell_ratio": 1.10,
            "futures_spread_pct": 0.02,
        },
    )

    assert label == "시장 품질 게이트"
    assert state == "reduced"
    assert "REDUCE" in detail


def test_utbreakout_shadow_candidate_resolves_to_diagnostic_event():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.utbreakout_shadow_pending = {}
    engine.utbreakout_shadow_resolved_keys = set()
    captured = []

    def _capture(symbol, status, event=None, extra=None):
        captured.append((symbol, status, event, extra))

    engine._record_utbreakout_diagnostic_event = _capture
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg["shadow_runner_exit_enabled"] = False
    plan = {
        "entry_price": 100,
        "stop_loss": 95,
        "take_profit": 110,
        "risk_distance": 5,
        "rr_multiple": 2.0,
        "decision_candle_ts": 1000,
        "entry_timeframe": "15m",
        "htf_timeframe": "1h",
    }

    pending = engine._register_utbreakout_shadow_candidate(
        "BTC/USDT",
        "long",
        {"decision_candle_ts": 1000},
        plan,
        cfg,
        {"id": 2, "name": "UT + ATR guard"},
    )
    assert pending is not None

    closed = pd.DataFrame(
        [
            {"timestamp": 1000, "open": 100, "high": 101, "low": 99, "close": 100},
            {"timestamp": 2000, "open": 100, "high": 111, "low": 100, "close": 110},
        ]
    )
    resolved = engine._update_utbreakout_shadow_triple_barrier("BTC/USDT", closed, cfg)

    assert resolved[0]["shadow_outcome"] == "tp"
    assert captured[0][2] == "shadow_outcome"
    assert captured[0][3]["code"] == "SHADOW_TP"
    assert engine.utbreakout_shadow_pending == {}


def test_utbreakout_shadow_candidate_logs_runner_diagnostic_event():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.utbreakout_shadow_pending = {}
    engine.utbreakout_shadow_resolved_keys = set()
    engine.utbreakout_runner_stats_cache = {}
    captured = []

    def _capture(symbol, status, event=None, extra=None):
        captured.append((symbol, status, event, extra))

    engine._record_utbreakout_diagnostic_event = _capture
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg.update({
        "shadow_runner_exit_enabled": True,
        "shadow_runner_max_bars": 8,
        "shadow_triple_barrier_max_bars": 8,
        "atr_length": 2,
        "partial_take_profit_r_multiple": 1.0,
        "partial_take_profit_ratio": 0.35,
        "atr_trailing_activation_r": 1.0,
        "runner_chandelier_lookback": 3,
        "runner_structure_lookback": 2,
        "runner_dynamic_multiplier_enabled": False,
    })
    plan = {
        "entry_price": 100,
        "stop_loss": 95,
        "take_profit": 110,
        "risk_distance": 5,
        "rr_multiple": 2.0,
        "decision_candle_ts": 1000,
        "entry_timeframe": "15m",
        "htf_timeframe": "1h",
    }

    pending = engine._register_utbreakout_shadow_candidate(
        "BTC/USDT",
        "long",
        {"decision_candle_ts": 1000},
        plan,
        cfg,
        {"id": 2, "name": "UT + ATR guard"},
    )
    assert pending is not None

    rows = [{"timestamp": 1000, "open": 100, "high": 101, "low": 99, "close": 100}]
    for idx in range(1, 9):
        close = 100 + idx * 1.2
        rows.append({
            "timestamp": 1000 + idx * 1000,
            "open": close - 0.4,
            "high": close + 1.5,
            "low": close - 1.0,
            "close": close,
        })
    closed = pd.DataFrame(rows)

    resolved = engine._update_utbreakout_shadow_triple_barrier("BTC/USDT", closed, cfg)

    event_names = [item[2] for item in captured]
    assert "shadow_outcome" in event_names
    assert "runner_shadow_outcome" in event_names
    assert any(str(extra["code"]).startswith("SHADOW_RUNNER_") for extra in resolved)
    assert engine.utbreakout_shadow_pending == {}


def test_place_tp_sl_orders_uses_partial_tp_quantity_and_full_sl_quantity():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "2", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])

    async def _get_position(symbol, use_cache=False):
        return pos

    engine.get_server_position = _get_position

    asyncio.run(
        engine._place_tp_sl_orders(
            "BTC/USDT",
            "long",
            100,
            "2",
            tp_distance=15,
            sl_distance=10,
            tp_qty_ratio=0.5,
        )
    )

    stop_order = next(order for order in engine.exchange.created if order["type"] == "stop_market")
    tp_order = next(order for order in engine.exchange.created if order["type"] == "limit")
    assert float(stop_order["amount"]) == 2.0
    assert float(stop_order["params"]["stopPrice"]) == 90.0
    assert float(tp_order["amount"]) == 1.0
    assert float(tp_order["price"]) == 115.0


def test_utbreakout_trailing_replaces_sl_and_keeps_partial_tp_order():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine(
        [
            {
                "id": "tp-existing",
                "side": "sell",
                "type": "limit",
                "price": "115",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT"},
            },
            {
                "id": "sl-old",
                "side": "sell",
                "type": "market",
                "clientOrderId": "utbslBTCUSDTold",
                "info": {"origType": "STOP_MARKET", "stopPrice": "90", "reduceOnly": "true", "symbol": "BTCUSDT"},
            },
        ],
        positions=[pos],
    )
    engine.utbreakout_trailing_states = {
        "BTC/USDT": {
            "side": "long",
            "entry_price": 100.0,
            "initial_qty": 2.0,
            "remaining_ratio": 0.5,
            "risk_distance": 10.0,
            "activation_r": 1.5,
            "trailing_atr_multiplier": 1.0,
            "breakeven_enabled": True,
            "last_stop_price": 90.0,
            "active": False,
        }
    }
    rows = []
    for idx in range(25):
        close = 100 + idx * 1.5
        rows.append({
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        })
    df = pd.DataFrame(rows)

    state = asyncio.run(
        engine._manage_utbreakout_partial_trailing(
            "BTC/USDT",
            pos,
            df,
            {
                "atr_length": 14,
                "atr_trailing_enabled": True,
                "atr_trailing_multiplier": 1.0,
                "atr_trailing_breakeven_enabled": True,
            },
        )
    )

    assert state["active"] is True
    assert ("sl-old", "BTC/USDT") in engine.exchange.cancelled
    remaining_ids = {order["id"] for order in engine.exchange.orders}
    assert "tp-existing" in remaining_ids
    assert "sl-old" not in remaining_ids
    assert any(order["type"] == "stop_market" for order in engine.exchange.created)

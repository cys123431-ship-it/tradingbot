import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import emas
from trading_safety.order_state import OrderRecord, OrderState, SQLiteTradingStateStore


class PreflightExchange:
    id = "binance"

    def market(self, symbol):
        return {
            "id": "HMSTRUSDT",
            "precision": {"price": 0.0000001},
            "info": {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0000001"}]},
        }

    def fapiPrivateGetSymbolConfig(self, params):
        return [{"symbol": "HMSTRUSDT", "marginType": "ISOLATED", "leverage": "5"}]

    def fapiPrivateGetLeverageBracket(self, params):
        return [
            {
                "symbol": "HMSTRUSDT",
                "brackets": [
                    {
                        "notionalFloor": "0",
                        "notionalCap": "100000",
                        "maintMarginRatio": "0.005",
                    }
                ],
            }
        ]


def make_preflight_engine():
    engine = object.__new__(emas.SignalEngine)
    engine.exchange = PreflightExchange()
    engine.trading_state_store = SQLiteTradingStateStore(":memory:")
    engine.idempotent_order_gateway = SimpleNamespace(exchange=engine.exchange)
    engine._engine_performance_stats_restored = True
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_common_settings = lambda: {}
    return engine


def test_pre_entry_liquidation_conflict_blocks_before_order():
    async def scenario():
        engine = make_preflight_engine()
        result = await emas._preflight_liquidation_safety(
            engine, "HMSTR/USDT:USDT", "long", 0.0002339, 0.0001885, 5, 1000, {}
        )
        assert result["valid"] is False
        assert result["status"] == "ENTRY_REJECTED_LIQUIDATION_CONFLICT"

    asyncio.run(scenario())


def test_pre_entry_missing_risk_data_is_unknown_not_safe():
    async def scenario():
        engine = make_preflight_engine()
        engine.exchange.fapiPrivateGetLeverageBracket = None
        result = await emas._preflight_liquidation_safety(
            engine, "HMSTR/USDT:USDT", "long", 0.0002339, 0.00021, 5, 1000, {}
        )
        assert result["valid"] is False
        assert result["status"] == "LIQUIDATION_SAFETY_UNKNOWN"

    asyncio.run(scenario())


def test_optional_auto_reduce_never_increases_leverage_and_selects_highest_safe_value():
    async def scenario():
        engine = make_preflight_engine()
        result = await emas._preflight_liquidation_safety(
            engine,
            "HMSTR/USDT:USDT",
            "long",
            0.0002339,
            0.00018,
            5,
            1000,
            {"auto_reduce_leverage_for_liquidation_safety": True},
        )
        assert result["valid"] is True
        assert result["selected_leverage"] == 3
        assert result["selected_leverage"] <= 5

    asyncio.run(scenario())


def test_post_fill_conflict_locks_closes_reduce_only_and_pauses(monkeypatch):
    async def scenario():
        store = SQLiteTradingStateStore(":memory:")
        store.upsert(
        OrderRecord(
            client_order_id="entry-hmstr",
            symbol="HMSTR/USDT:USDT",
            side="LONG",
            strategy="UTBREAKOUT",
            signal_timestamp="1",
            requested_qty=1000,
            filled_qty=1000,
            order_state=OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
        )
        )
        exchange = SimpleNamespace(id="binance")
        engine = SimpleNamespace(
        exchange=exchange,
        trading_state_store=store,
        idempotent_order_gateway=SimpleNamespace(exchange=exchange),
        _engine_performance_stats_restored=True,
        ctrl=SimpleNamespace(
            notify=AsyncMock(),
            format_symbol_for_display=lambda symbol: symbol,
            is_paused=False,
        ),
        _set_crypto_entry_lock=lambda reason: setattr(engine, "crypto_entry_lock_reason", reason),
        _protection_order_info=lambda value: value.get("info", {}) if isinstance(value, dict) else {},
        _position_signed_contracts=lambda value: value.get("contracts", 0),
        _cancel_protection_orders=AsyncMock(return_value=1),
        _emergency_close_position_without_stop_loss=AsyncMock(
            return_value={"status": "EMERGENCY_CLOSED", "closed": True}
        ),
        )
        monkeypatch.setattr(emas, "write_critical_pause_state", lambda *args, **kwargs: {})
        result = await emas._handle_liquidation_safety_failure(
            engine,
            "HMSTR/USDT:USDT",
            {
                "side": "long",
                "contracts": 1000,
                "markPrice": 0.0002,
                "liquidationPrice": 0.0001891,
            },
            "LONG_STOP_BELOW_LIQUIDATION",
            stop_price=0.0001885,
            working_type="MARK_PRICE",
            client_order_id="entry-hmstr",
        )
        assert result["closed"] is True
        engine._emergency_close_position_without_stop_loss.assert_awaited_once()
        kwargs = engine._emergency_close_position_without_stop_loss.await_args.kwargs
        assert kwargs["protection_label"] == "LIQUIDATION_SAFETY"
        assert engine.ctrl.is_paused is True
        assert "CRITICAL_PAUSE" in engine.crypto_entry_lock_reason

    asyncio.run(scenario())


def test_emergency_close_failure_persists_explicit_state(monkeypatch):
    async def scenario():
        store = SQLiteTradingStateStore(":memory:")
        store.upsert(
        OrderRecord(
            client_order_id="entry-failed-close",
            symbol="HMSTR/USDT:USDT",
            side="LONG",
            strategy="UTBREAKOUT",
            signal_timestamp="1",
            requested_qty=1000,
            filled_qty=1000,
            order_state=OrderState.FILLED_LIQUIDATION_CONFLICT.value,
        )
        )
        exchange = SimpleNamespace(id="binance")
        engine = SimpleNamespace(
        exchange=exchange,
        trading_state_store=store,
        idempotent_order_gateway=SimpleNamespace(exchange=exchange),
        _engine_performance_stats_restored=True,
        ctrl=SimpleNamespace(notify=AsyncMock(), format_symbol_for_display=str, is_paused=False),
        _set_crypto_entry_lock=lambda reason: setattr(engine, "crypto_entry_lock_reason", reason),
        _protection_order_info=lambda value: {},
        _position_signed_contracts=lambda value: 1000,
        _cancel_protection_orders=AsyncMock(return_value=0),
        _emergency_close_position_without_stop_loss=AsyncMock(
            return_value={"status": "CRITICAL_PAUSED", "closed": False, "error": "timeout"}
        ),
        )
        monkeypatch.setattr(emas, "write_critical_pause_state", lambda *args, **kwargs: {})
        await emas._handle_liquidation_safety_failure(
            engine,
            "HMSTR/USDT:USDT",
            {"side": "long", "contracts": 1000},
            "LONG_STOP_BELOW_LIQUIDATION",
            stop_price=0.0001885,
        )
        assert store.get("entry-failed-close").order_state == OrderState.EMERGENCY_CLOSE_FAILED.value

    asyncio.run(scenario())

import asyncio

import pytest

from trading_safety.order_gateway import IdempotentOrderGateway
from trading_safety.order_state import OrderState, SQLiteTradingStateStore


class MockExchange:
    def __init__(self, behavior="filled"):
        self.behavior = behavior
        self.orders = {}
        self.create_count = 0
        self.cancel_count = 0
        self.position = None

    def fapiPrivateGetOrder(self, params):
        return self.orders.get(params["origClientOrderId"])

    def fetch_positions(self, symbols=None):
        return [self.position] if self.position else []

    def create_order(self, symbol, order_type, side, qty, price, params):
        self.create_count += 1
        client_id = params["newClientOrderId"]
        if self.behavior == "timeout_not_received":
            raise TimeoutError("request timed out before exchange receipt")
        status = "PARTIALLY_FILLED" if self.behavior == "partial" else "FILLED"
        filled = qty * 0.3 if self.behavior == "partial" else qty
        order = {
            "id": str(self.create_count),
            "clientOrderId": client_id,
            "symbol": symbol,
            "status": status,
            "amount": qty,
            "filled": filled,
            "average": 100.0,
        }
        self.orders[client_id] = order
        if self.behavior == "timeout_after_fill":
            self.position = {
                "symbol": symbol,
                "side": "long" if side == "buy" else "short",
                "contracts": filled,
                "entryPrice": 100.0,
            }
            raise TimeoutError("HTTP response lost after fill")
        return order

    def cancel_order(self, order_id, symbol):
        self.cancel_count += 1
        return {"id": order_id, "symbol": symbol, "status": "canceled"}


def _gateway(tmp_path, exchange):
    return IdempotentOrderGateway(
        exchange,
        SQLiteTradingStateStore(tmp_path / "state.sqlite3"),
        recovery_delays=(0.0,),
    )


def test_same_signal_submits_only_once(tmp_path):
    async def scenario():
        exchange = MockExchange()
        gateway = _gateway(tmp_path, exchange)
        kwargs = {
            "strategy": "UTB",
            "symbol": "BTC/USDT:USDT",
            "side": "LONG",
            "signal_timestamp": 1000,
            "qty": 0.1,
        }

        first = await gateway.submit_entry(**kwargs)
        second = await gateway.submit_entry(**kwargs)

        assert first.accepted is True
        assert second.recovered is True
        assert exchange.create_count == 1

    asyncio.run(scenario())


def test_timeout_after_exchange_fill_recovers_existing_order(tmp_path):
    async def scenario():
        exchange = MockExchange("timeout_after_fill")
        gateway = _gateway(tmp_path, exchange)
        result = await gateway.submit_entry(
            strategy="UTB", symbol="BTC/USDT:USDT", side="LONG", signal_timestamp=1000, qty=0.1
        )
        assert result.recovered is True
        assert result.state == OrderState.FILLED_UNVERIFIED_LIQUIDATION.value
        assert exchange.create_count == 1

    asyncio.run(scenario())


def test_timeout_without_exchange_receipt_remains_unknown_and_blocks(tmp_path):
    async def scenario():
        exchange = MockExchange("timeout_not_received")
        gateway = _gateway(tmp_path, exchange)
        result = await gateway.submit_entry(
            strategy="UTB", symbol="BTC/USDT:USDT", side="LONG", signal_timestamp=1000, qty=0.1
        )
        second = await gateway.submit_entry(
            strategy="UTB", symbol="ETH/USDT:USDT", side="LONG", signal_timestamp=1000, qty=0.1
        )
        assert result.state == OrderState.SUBMITTED_UNKNOWN.value
        assert second.state == "BLOCKED"
        assert exchange.create_count == 1

    asyncio.run(scenario())


def test_partial_fill_uses_actual_filled_quantity(tmp_path):
    async def scenario():
        exchange = MockExchange("partial")
        gateway = _gateway(tmp_path, exchange)
        result = await gateway.submit_entry(
            strategy="UTB", symbol="BTC/USDT:USDT", side="LONG", signal_timestamp=1000, qty=1.0
        )
        record = gateway.store.get(result.client_order_id)
        assert result.state == OrderState.PARTIALLY_FILLED.value
        assert record.filled_qty == pytest.approx(0.3)
        assert gateway.store.entry_block_reason() is not None
        assert exchange.cancel_count == 1

    asyncio.run(scenario())


def test_reduce_only_close_is_idempotent_and_cannot_reverse(tmp_path):
    async def scenario():
        exchange = MockExchange()
        exchange.position = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.1, "entryPrice": 100.0}
        gateway = _gateway(tmp_path, exchange)
        first = await gateway.submit_reduce_only_close(
            strategy="UTB", symbol="BTC/USDT:USDT", position_side="long",
            position_signature="entry-1", qty=0.1, reason="manual",
        )
        second = await gateway.submit_reduce_only_close(
            strategy="UTB", symbol="BTC/USDT:USDT", position_side="long",
            position_signature="entry-1", qty=0.1, reason="manual",
        )
        order = exchange.orders[first.client_order_id]
        assert order["clientOrderId"] == first.client_order_id
        assert exchange.create_count == 1
        assert second.recovered is True

    asyncio.run(scenario())


def test_pre_submit_order_lookup_failure_blocks_without_order(tmp_path):
    async def scenario():
        exchange = MockExchange()

        def failed_lookup(params):
            raise TimeoutError("lookup unavailable")

        exchange.fapiPrivateGetOrder = failed_lookup
        gateway = _gateway(tmp_path, exchange)
        result = await gateway.submit_entry(
            strategy="UTB", symbol="BTC/USDT:USDT", side="LONG", signal_timestamp=1000, qty=0.1
        )
        assert result.state == OrderState.SUBMITTED_UNKNOWN.value
        assert exchange.create_count == 0
        assert gateway.store.get_runtime_state("entry_lock_reason") == "RECONCILIATION_REQUIRED"

    asyncio.run(scenario())


def test_same_position_add_signal_submits_only_once(tmp_path):
    class AddExchange(MockExchange):
        def create_order(self, symbol, order_type, side, qty, price, params):
            order = super().create_order(symbol, order_type, side, qty, price, params)
            self.position["contracts"] += float(qty)
            return order

    async def scenario():
        exchange = AddExchange()
        exchange.position = {
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 1.0,
            "entryPrice": 100.0,
        }
        gateway = _gateway(tmp_path, exchange)
        kwargs = {
            "strategy": "AGGRESSIVE_GROWTH_PYRAMID",
            "symbol": "BTC/USDT:USDT",
            "side": "LONG",
            "signal_timestamp": 1000,
            "qty": 0.5,
            "stage": "1",
        }
        first = await gateway.submit_position_add(**kwargs)
        second = await gateway.submit_position_add(**kwargs)

        assert first.state == OrderState.FILLED_UNVERIFIED_LIQUIDATION.value
        assert second.recovered is True
        assert exchange.create_count == 1
        assert exchange.position["contracts"] == pytest.approx(1.5)

    asyncio.run(scenario())


def test_authentication_error_persists_global_entry_lock(tmp_path):
    class AuthenticationError(Exception):
        pass

    async def scenario():
        exchange = MockExchange()

        def rejected(*args, **kwargs):
            exchange.create_count += 1
            raise AuthenticationError("invalid api key")

        exchange.create_order = rejected
        gateway = _gateway(tmp_path, exchange)
        result = await gateway.submit_entry(
            strategy="UTB", symbol="BTC/USDT:USDT", side="LONG", signal_timestamp=1000, qty=0.1
        )
        assert result.state == OrderState.FAILED.value
        assert gateway.store.get_runtime_state("entry_lock_reason").startswith("AUTHENTICATION_ERROR")

    asyncio.run(scenario())

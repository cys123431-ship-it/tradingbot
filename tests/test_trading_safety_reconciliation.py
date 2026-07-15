import asyncio

from trading_safety.order_state import OrderRecord, OrderState, SQLiteTradingStateStore
from trading_safety.reconciliation import reconcile_exchange_state


class ReconcileExchange:
    def __init__(self, positions=None, orders=None):
        self.positions = positions or []
        self.orders = orders or []

    def fetch_positions(self):
        return self.positions

    def fetch_open_orders(self):
        return self.orders


def test_exchange_position_without_local_record_blocks_startup(tmp_path):
    async def scenario():
        exchange = ReconcileExchange(
            positions=[{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1, "entryPrice": 100}],
            orders=[{"symbol": "BTC/USDT:USDT", "type": "STOP_MARKET", "reduceOnly": True, "id": "sl-1"}],
        )
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        result = await reconcile_exchange_state(exchange, store)
        assert result.safe_to_trade is False
        assert any("without_local_record" in issue for issue in result.issues)
        assert store.list_by_states({OrderState.FILLED_UNPROTECTED})

    asyncio.run(scenario())


def test_unprotected_position_blocks_and_verified_stop_recovers(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "cid-1", "BTC/USDT:USDT", "LONG", "UTB", "1", 1.0,
                order_state=OrderState.FILLED_UNPROTECTED.value,
            )
        )
        position = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1, "entryPrice": 100}
        blocked = await reconcile_exchange_state(ReconcileExchange([position], []), store)
        recovered = await reconcile_exchange_state(
            ReconcileExchange(
                [position],
                [{"symbol": "BTC/USDT:USDT", "type": "STOP_MARKET", "reduceOnly": True, "id": "sl-1"}],
            ),
            store,
        )
        assert blocked.safe_to_trade is False
        assert recovered.safe_to_trade is True
        assert store.get("cid-1").order_state == OrderState.PROTECTED.value

    asyncio.run(scenario())


def test_local_active_without_exchange_position_requires_reconciliation(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "cid-1", "BTC/USDT:USDT", "LONG", "UTB", "1", 1.0,
                order_state=OrderState.PROTECTED.value,
            )
        )
        result = await reconcile_exchange_state(ReconcileExchange([], []), store)
        assert result.safe_to_trade is False
        assert any("local_active_without_exchange_position" in issue for issue in result.issues)
        assert store.get("cid-1").order_state == OrderState.PROTECTED.value

    asyncio.run(scenario())


def test_binance_terminal_entry_record_closes_when_exchange_position_is_flat(tmp_path):
    class BinanceFlatExchange:
        id = "binance"

        def fetch_positions(self):
            return []

        def fetch_open_orders(self):
            return []

        def fapiPrivateGetOpenAlgoOrders(self, params):
            return []

        def fapiPrivateGetOrder(self, params):
            assert params["symbol"] == "LTCUSDC"
            return {
                "symbol": "LTCUSDC",
                "clientOrderId": params["origClientOrderId"],
                "status": "FILLED",
            }

    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "cid-usdc",
                "LTC/USDC:USDC",
                "LONG",
                "UTB",
                "1",
                1.0,
                order_state=OrderState.PROTECTED.value,
            )
        )

        result = await reconcile_exchange_state(BinanceFlatExchange(), store)

        assert result.safe_to_trade is True
        record = store.get("cid-usdc")
        assert record.order_state == OrderState.CLOSED.value
        assert record.metadata["reconciled_terminal_order_status"] == "FILLED"

    asyncio.run(scenario())


def test_startup_closes_synthetic_record_after_confirmed_emergency_close(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "recon-hmstr", "HMSTR/USDT:USDT", "LONG",
                "EXTERNAL_OR_PRE_RECONCILIATION", "1", 10.0,
                filled_qty=10.0,
                order_state=OrderState.FILLED_UNPROTECTED.value,
                created_at="2026-07-11T12:53:15+00:00",
                updated_at="2026-07-11T12:53:15+00:00",
            )
        )
        store.upsert(
            OrderRecord(
                "emerg-hmstr", "HMSTR/USDT", "LONG",
                "EMERGENCY_PROTECTION_CLOSE", "2", 10.0,
                filled_qty=10.0,
                order_state=OrderState.CLOSED.value,
                created_at="2026-07-11T12:53:21+00:00",
                updated_at="2026-07-11T12:53:22+00:00",
            )
        )

        result = await reconcile_exchange_state(ReconcileExchange([], []), store)

        assert result.safe_to_trade is True
        assert store.get("recon-hmstr").order_state == OrderState.CLOSED.value
        assert (
            store.get("recon-hmstr").metadata["reconciled_by_close_client_order_id"]
            == "emerg-hmstr"
        )

    asyncio.run(scenario())


def test_oversized_reduce_only_order_blocks_startup(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        position = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1, "entryPrice": 100}
        store.upsert(
            OrderRecord(
                "cid-1", "BTC/USDT:USDT", "LONG", "UTB", "1", 1.0,
                order_state=OrderState.PROTECTED.value,
            )
        )
        orders = [
            {"symbol": "BTC/USDT:USDT", "type": "STOP_MARKET", "reduceOnly": True, "amount": 1, "id": "sl"},
            {"symbol": "BTC/USDT:USDT", "type": "LIMIT", "reduceOnly": True, "amount": 1.1, "id": "tp"},
        ]
        result = await reconcile_exchange_state(ReconcileExchange([position], orders), store)
        assert result.safe_to_trade is False
        assert any("reduce_only_qty_exceeds_position" in issue for issue in result.issues)

    asyncio.run(scenario())

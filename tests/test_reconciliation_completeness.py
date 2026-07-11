import asyncio

from trading_safety.order_state import OrderRecord, OrderState, SQLiteTradingStateStore
from trading_safety.reconciliation import reconcile_exchange_state


class BinanceSnapshotExchange:
    id = "binance"

    def __init__(self, *, algo_error=False):
        self.algo_error = algo_error

    def fetch_positions(self):
        return []

    def fetch_open_orders(self):
        return []

    def fapiPrivateGetOpenAlgoOrders(self, _params):
        if self.algo_error:
            raise TimeoutError("algo snapshot timeout")
        return []


def test_algo_snapshot_failure_blocks_safe_to_trade(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        result = await reconcile_exchange_state(BinanceSnapshotExchange(algo_error=True), store)
        assert result.snapshot_complete is False
        assert result.algo_orders_ok is False
        assert result.safe_to_trade is False

    asyncio.run(scenario())


def test_user_stream_requirement_is_independent_of_rest_snapshot(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        result = await reconcile_exchange_state(
            BinanceSnapshotExchange(),
            store,
            user_stream_ready=False,
            require_user_stream=True,
        )
        assert result.snapshot_complete is True
        assert result.user_stream_ready is False
        assert result.safe_to_trade is False

    asyncio.run(scenario())


def test_acknowledged_unknown_lookup_is_unresolved(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "entry-1",
                "BTC/USDT:USDT",
                "LONG",
                "UTB",
                "1",
                1.0,
                order_state=OrderState.ACKNOWLEDGED.value,
            )
        )
        exchange = BinanceSnapshotExchange()
        result = await reconcile_exchange_state(exchange, store)
        assert result.safe_to_trade is False
        assert any("entry-1" in value for value in result.unresolved_records)

    asyncio.run(scenario())


def test_protected_local_record_still_requires_individual_binance_lookup(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert(
            OrderRecord(
                "entry-protected",
                "BTC/USDT:USDT",
                "LONG",
                "UTB",
                "1",
                1.0,
                order_state=OrderState.PROTECTED.value,
            )
        )
        result = await reconcile_exchange_state(BinanceSnapshotExchange(), store)
        assert result.safe_to_trade is False
        assert any("entry-protected" in value for value in result.unresolved_records)

    asyncio.run(scenario())

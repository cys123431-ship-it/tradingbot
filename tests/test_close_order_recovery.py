import asyncio

from trading_safety.order_gateway import IdempotentOrderGateway
from trading_safety.order_state import OrderIntent, OrderRecord, OrderState, SQLiteTradingStateStore


class LookupExchange:
    def __init__(self, order=None):
        self.order = order

    def fetch_open_orders(self, _symbol):
        return [self.order] if self.order else []


def _close_record(store, *, before=100.0):
    record = OrderRecord(
        "close-1",
        "BTC/USDT:USDT",
        "LONG",
        "TEST",
        "position-1",
        before,
        order_state=OrderState.SUBMITTED_UNKNOWN.value,
        order_intent=OrderIntent.CLOSE.value,
        order_purpose="close_manual",
        position_qty_before=before,
        position_signature="position-1",
    )
    store.upsert(record)
    return record


def test_close_not_found_with_unchanged_position_remains_unknown(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        record = _close_record(store)

        async def position(_symbol):
            return {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 100}

        gateway = IdempotentOrderGateway(LookupExchange(), store, position_fetcher=position)
        result = await gateway.recover(record, wait=False)
        assert result.state == OrderState.SUBMITTED_UNKNOWN.value
        assert store.get("close-1").order_state == OrderState.SUBMITTED_UNKNOWN.value

    asyncio.run(scenario())


def test_close_recovery_confirms_flat_and_partial_quantities(tmp_path):
    async def scenario():
        path = tmp_path / "state.sqlite3"
        store = SQLiteTradingStateStore(path)
        record = _close_record(store)
        remaining = {"qty": 40.0}

        async def position(_symbol):
            qty = remaining["qty"]
            return None if qty == 0 else {"side": "long", "contracts": qty}

        gateway = IdempotentOrderGateway(LookupExchange(), store, position_fetcher=position)
        partial = await gateway.recover(record, wait=False)
        assert partial.state == OrderState.PARTIALLY_CLOSED.value
        assert store.get("close-1").filled_qty == 60.0

        remaining["qty"] = 0
        closed = await gateway.recover(store.get("close-1"), wait=False)
        assert closed.state == OrderState.CLOSED.value

    asyncio.run(scenario())


def test_close_recovery_detects_reversed_position(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        record = _close_record(store)

        async def position(_symbol):
            return {"side": "short", "contracts": 10}

        gateway = IdempotentOrderGateway(LookupExchange(), store, position_fetcher=position)
        result = await gateway.recover(record, wait=False)
        assert result.state == OrderState.SUBMITTED_UNKNOWN.value
        assert "close_reversed_position" in result.error
        assert store.get_runtime_state("entry_lock_reason").startswith("CRITICAL_PAUSE")

    asyncio.run(scenario())

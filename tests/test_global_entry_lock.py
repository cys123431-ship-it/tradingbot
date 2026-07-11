import asyncio
import time

from trading_safety.order_gateway import IdempotentOrderGateway
from trading_safety.order_state import SQLiteTradingStateStore


class SlowEntryExchange:
    def __init__(self):
        self.create_calls = 0

    def fetch_open_orders(self, _symbol):
        return []

    def create_order(self, symbol, _kind, _side, qty, _price, params):
        self.create_calls += 1
        time.sleep(0.05)
        return {
            "id": str(self.create_calls),
            "symbol": symbol,
            "status": "open",
            "amount": qty,
            "filled": 0,
            "clientOrderId": params["newClientOrderId"],
        }


def test_different_symbols_are_serialized_in_single_position_mode(tmp_path):
    async def scenario():
        exchange = SlowEntryExchange()
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")

        async def no_position(_symbol):
            return None

        gateway = IdempotentOrderGateway(exchange, store, position_fetcher=no_position)
        results = await asyncio.gather(
            gateway.submit_entry(
                strategy="UTB", symbol="BTC/USDT", side="long", signal_timestamp=1, qty=0.1
            ),
            gateway.submit_entry(
                strategy="RSPT", symbol="ETH/USDT", side="long", signal_timestamp=1, qty=0.1
            ),
        )

        assert exchange.create_calls == 1
        assert sorted(result.state == "BLOCKED" for result in results) == [False, True]

    asyncio.run(scenario())


def test_sqlite_lease_serializes_separate_gateway_instances(tmp_path):
    async def scenario():
        exchange = SlowEntryExchange()
        path = tmp_path / "state.sqlite3"
        first_store = SQLiteTradingStateStore(path)
        second_store = SQLiteTradingStateStore(path)

        async def no_position(_symbol):
            return None

        first = IdempotentOrderGateway(exchange, first_store, position_fetcher=no_position)
        second = IdempotentOrderGateway(exchange, second_store, position_fetcher=no_position)
        results = await asyncio.gather(
            first.submit_entry(
                strategy="UTB", symbol="BTC/USDT", side="long", signal_timestamp=1, qty=0.1
            ),
            second.submit_entry(
                strategy="RSPT", symbol="ETH/USDT", side="long", signal_timestamp=1, qty=0.1
            ),
        )

        assert exchange.create_calls == 1
        assert sum(result.state == "BLOCKED" for result in results) == 1

    asyncio.run(scenario())

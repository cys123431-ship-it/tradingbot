import asyncio

from trading_safety.order_state import OrderRecord, OrderState, SQLiteTradingStateStore
from trading_safety.reconciliation import reconcile_exchange_state


class FakeExchange:
    id = "binance"

    def market(self, symbol):
        return {
            "precision": {"price": 0.0000001},
            "info": {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0000001"}]},
        }


def position(liquidation="0.0001891"):
    return {
        "symbol": "HMSTR/USDT:USDT",
        "side": "long",
        "contracts": 1000,
        "entryPrice": 0.0002339,
        "liquidationPrice": liquidation,
        "info": {"positionAmt": "1000", "liquidationPrice": liquidation},
    }


def stop(price="0.0001885", working="MARK_PRICE", qty=1000):
    return {
        "id": "sl-1",
        "symbol": "HMSTR/USDT:USDT",
        "type": "STOP_MARKET",
        "side": "sell",
        "amount": qty,
        "triggerPrice": price,
        "workingType": working,
        "reduceOnly": True,
    }


def test_restart_blocks_position_with_stop_below_liquidation():
    async def scenario():
        store = SQLiteTradingStateStore(":memory:")
        store.upsert(
        OrderRecord(
            client_order_id="entry-1",
            symbol="HMSTR/USDT:USDT",
            side="LONG",
            strategy="UTBREAKOUT",
            signal_timestamp="1",
            requested_qty=1000,
            filled_qty=1000,
            order_state=OrderState.FILLED_UNPROTECTED.value,
        )
        )
        result = await reconcile_exchange_state(
            FakeExchange(),
            store,
            position_fetcher=lambda: _async([position()]),
            open_orders_fetcher=lambda: _async([stop()]),
        )
        assert result.safe_to_trade is False
        assert any("LONG_STOP_BELOW_LIQUIDATION" in issue for issue in result.issues)
        assert store.get("entry-1").order_state == OrderState.FILLED_UNPROTECTED.value

    asyncio.run(scenario())


def test_restart_accepts_wide_contract_price_stop_but_rejects_missing_liquidation():
    async def scenario():
        external_stop = stop("0.00021", "CONTRACT_PRICE")
        external_stop["closePosition"] = True
        external_stop["amount"] = 0
        external_stop["reduceOnly"] = False
        store = SQLiteTradingStateStore(":memory:")
        store.upsert(
            OrderRecord(
                client_order_id="entry-external-stop",
                symbol="HMSTR/USDT:USDT",
                side="LONG",
                strategy="UTBREAKOUT",
                signal_timestamp="1",
                requested_qty=1000,
                filled_qty=1000,
                order_state=OrderState.FILLED_UNPROTECTED.value,
            )
        )
        accepted = await reconcile_exchange_state(
            FakeExchange(),
            store,
            position_fetcher=lambda: _async([position()]),
            open_orders_fetcher=lambda: _async([external_stop]),
        )
        assert accepted.safe_to_trade is True
        assert store.get("entry-external-stop").order_state == OrderState.PROTECTED.value

        missing = await reconcile_exchange_state(
            FakeExchange(),
            SQLiteTradingStateStore(":memory:"),
            position_fetcher=lambda: _async([position("0")]),
            open_orders_fetcher=lambda: _async([stop("0.00021")]),
        )
        assert missing.safe_to_trade is False
        assert any("liquidation_price_unavailable" in value for value in missing.issues)

    asyncio.run(scenario())


def test_restart_accepts_full_qty_mark_price_stop_with_safe_buffer():
    async def scenario():
        store = SQLiteTradingStateStore(":memory:")
        store.upsert(
        OrderRecord(
            client_order_id="entry-safe",
            symbol="HMSTR/USDT:USDT",
            side="LONG",
            strategy="UTBREAKOUT",
            signal_timestamp="1",
            requested_qty=1000,
            filled_qty=1000,
            order_state=OrderState.FILLED_UNPROTECTED.value,
        )
        )
        result = await reconcile_exchange_state(
            FakeExchange(),
            store,
            position_fetcher=lambda: _async([position()]),
            open_orders_fetcher=lambda: _async([stop("0.00021")]),
        )
        assert result.safe_to_trade is True
        assert store.get("entry-safe").order_state == OrderState.PROTECTED.value

    asyncio.run(scenario())


async def _async(value):
    return value

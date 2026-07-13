import asyncio

from trading_safety.binance_algo_gateway import BinanceAlgoOrderGateway


class FakeBinance:
    id = "binance"

    def __init__(self):
        self.params = None

    def market(self, symbol):
        return {"id": "HMSTRUSDT"}

    def fapiPrivatePostAlgoOrder(self, params):
        self.params = dict(params)
        return {
            "algoId": 123,
            "clientAlgoId": params["clientAlgoId"],
            "symbol": params["symbol"],
            "side": params["side"],
            "orderType": params["type"],
            "quantity": params["quantity"],
            "triggerPrice": params["triggerPrice"],
            "workingType": params["workingType"],
            "priceProtect": params["priceProtect"],
            "reduceOnly": params["reduceOnly"],
            "algoStatus": "NEW",
        }


def test_stop_market_uses_binance_algo_service_and_mark_price():
    async def scenario():
        exchange = FakeBinance()
        order = await BinanceAlgoOrderGateway(exchange).create_conditional_order(
            "HMSTR/USDT:USDT",
            "STOP_MARKET",
            "sell",
            "1000",
            trigger_price="0.0002",
            client_algo_id="utbslhmstr123",
            reduce_only=True,
            working_type="MARK_PRICE",
            price_protect=False,
        )
        assert exchange.params["algoType"] == "CONDITIONAL"
        assert exchange.params["workingType"] == "MARK_PRICE"
        assert exchange.params["priceProtect"] == "false"
        assert exchange.params["reduceOnly"] == "true"
        assert exchange.params["triggerPrice"] == "0.0002"
        assert order["_protection_source"] == "binance_algo"

    asyncio.run(scenario())


def test_close_position_market_trigger_omits_quantity_and_reduce_only():
    async def scenario():
        exchange = FakeBinance()
        await BinanceAlgoOrderGateway(exchange).create_conditional_order(
            "HMSTR/USDT:USDT",
            "STOP_MARKET",
            "sell",
            None,
            trigger_price="0.0002",
            client_algo_id="closeallhmstr123",
            close_position=True,
        )
        assert exchange.params["closePosition"] == "true"
        assert "quantity" not in exchange.params
        assert "reduceOnly" not in exchange.params

    # FakeBinance's response fixture expects normal-order-only keys. Return a minimal
    # valid Algo response for this close-all request instead.
    exchange_method = FakeBinance.fapiPrivatePostAlgoOrder

    def close_all_response(self, params):
        self.params = dict(params)
        return {
            "algoId": 456,
            "clientAlgoId": params["clientAlgoId"],
            "symbol": params["symbol"],
            "side": params["side"],
            "orderType": params["type"],
            "triggerPrice": params["triggerPrice"],
            "closePosition": params["closePosition"],
            "algoStatus": "NEW",
        }

    FakeBinance.fapiPrivatePostAlgoOrder = close_all_response
    try:
        asyncio.run(scenario())
    finally:
        FakeBinance.fapiPrivatePostAlgoOrder = exchange_method


def test_close_position_rejects_invalid_parameter_combinations():
    async def scenario():
        gateway = BinanceAlgoOrderGateway(FakeBinance())
        try:
            await gateway.create_conditional_order(
                "HMSTR/USDT:USDT",
                "STOP",
                "sell",
                None,
                trigger_price="0.0002",
                client_algo_id="badclose1",
                close_position=True,
            )
        except ValueError as exc:
            assert "closePosition=true" in str(exc)
        else:
            raise AssertionError("invalid closePosition order type must be rejected")

        try:
            await gateway.create_conditional_order(
                "HMSTR/USDT:USDT",
                "STOP_MARKET",
                "sell",
                None,
                trigger_price="0.0002",
                client_algo_id="badclose2",
                close_position=True,
                price="0.0001",
            )
        except ValueError as exc:
            assert "must not include price" in str(exc)
        else:
            raise AssertionError("closePosition market trigger with price must be rejected")

    asyncio.run(scenario())


def test_normalize_preserves_manual_close_position_fields():
    normalized = BinanceAlgoOrderGateway.normalize({
        "algoId": 77,
        "clientAlgoId": "manual-stop",
        "symbol": "KORUUSDT",
        "orderType": "STOP_MARKET",
        "side": "BUY",
        "triggerPrice": "467.14",
        "closePosition": "true",
        "positionSide": "BOTH",
        "workingType": "CONTRACT_PRICE",
        "algoStatus": "NEW",
    })
    assert normalized["closePosition"] is True
    assert normalized["positionSide"] == "BOTH"
    assert normalized["triggerPrice"] == "467.14"

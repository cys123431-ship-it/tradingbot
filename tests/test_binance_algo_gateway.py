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

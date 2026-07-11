import asyncio

from trading_safety.binance_algo_gateway import AlgoLookupStatus, BinanceAlgoOrderGateway


class AlgoExchange:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def fapiPrivateGetAlgoOrder(self, _params):
        if self.error:
            raise self.error
        return self.response


def test_algo_lookup_found_not_found_and_unknown():
    async def scenario():
        found = await BinanceAlgoOrderGateway(
            AlgoExchange({"algoId": 1, "clientAlgoId": "sl-1", "algoStatus": "NEW"})
        ).fetch_by_client_id("sl-1")
        missing = await BinanceAlgoOrderGateway(
            AlgoExchange(error=RuntimeError("-2013 Order does not exist"))
        ).fetch_by_client_id("sl-1")
        timeout = await BinanceAlgoOrderGateway(
            AlgoExchange(error=TimeoutError("network timeout"))
        ).fetch_by_client_id("sl-1")
        unavailable = await BinanceAlgoOrderGateway(object()).fetch_by_client_id("sl-1")

        assert found.status is AlgoLookupStatus.FOUND
        assert missing.status is AlgoLookupStatus.NOT_FOUND
        assert timeout.status is AlgoLookupStatus.UNKNOWN
        assert unavailable.status is AlgoLookupStatus.UNKNOWN

    asyncio.run(scenario())


def test_open_algo_snapshot_requires_supported_complete_endpoint():
    async def scenario():
        unsupported = await BinanceAlgoOrderGateway(object()).fetch_open_orders()
        invalid = await BinanceAlgoOrderGateway(AlgoExchange()).fetch_open_orders()
        assert unsupported.ok is False
        assert invalid.ok is False

    asyncio.run(scenario())

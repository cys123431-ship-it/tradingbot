import asyncio

import emas


def test_protection_timeout_recovers_by_client_order_id_without_resubmit():
    orders = []

    class Exchange:
        def __init__(self):
            self.calls = 0

        def create_order(self, symbol, order_type, side, qty, price, params):
            self.calls += 1
            order = {
                "id": "sl-1",
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "amount": qty,
                "clientOrderId": params["newClientOrderId"],
                "info": {"clientOrderId": params["newClientOrderId"], "reduceOnly": True},
            }
            orders.append(order)
            raise TimeoutError("response lost after order acceptance")

    engine = object.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine._protection_client_order_id = lambda order: order.get("clientOrderId", "")

    async def collect(symbol):
        return True, list(orders)

    engine._collect_protection_orders_checked = collect
    result = asyncio.run(
        engine._create_protection_order_with_retries(
            "BTC/USDT:USDT",
            "stop_market",
            "sell",
            0.1,
            None,
            {"stopPrice": 90.0, "reduceOnly": True, "newClientOrderId": "utbsl-test"},
            "SL",
            max_attempts=3,
            retry_delay_sec=0,
        )
    )

    assert result["id"] == "sl-1"
    assert engine.exchange.calls == 1


def test_terminal_binance_algo_order_is_not_recovered_as_open_protection():
    class Exchange:
        id = "binance"

        def __init__(self):
            self.submit_calls = 0

        def market(self, _symbol):
            return {"id": "BTCUSDT"}

        def fapiPrivateGetAlgoOrder(self, params):
            return {
                "algoId": "cancelled-sl",
                "clientAlgoId": params["clientAlgoId"],
                "algoStatus": "CANCELED",
                "type": "STOP_MARKET",
            }

        def fapiPrivatePostAlgoOrder(self, params):
            self.submit_calls += 1
            return {
                "algoId": "new-sl",
                "clientAlgoId": params["clientAlgoId"],
                "algoStatus": "NEW",
                "type": "STOP_MARKET",
                "triggerPrice": params["triggerPrice"],
            }

    engine = object.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine._set_crypto_entry_lock = lambda _reason: None
    engine._collect_protection_orders_checked = lambda _symbol: asyncio.sleep(
        0,
        result=(True, []),
    )

    result = asyncio.run(
        engine._create_protection_order_with_retries(
            "BTC/USDT:USDT",
            "stop_market",
            "sell",
            0.1,
            None,
            {
                "stopPrice": 90.0,
                "reduceOnly": True,
                "newClientOrderId": "utbsl-terminal-test",
            },
            "SL",
            max_attempts=1,
            retry_delay_sec=0,
        )
    )

    assert result["id"] == "new-sl"
    assert engine.exchange.submit_calls == 1

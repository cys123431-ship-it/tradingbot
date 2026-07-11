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

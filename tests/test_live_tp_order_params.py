import pytest
import emas
import asyncio


def test_tp_limit_order_uses_reduce_only_not_close_position(monkeypatch):
    async def run():
        cls = emas.MainController
        bot = cls.__new__(cls)

        calls = []

        async def fake_create(*args, **kwargs):
            calls.append((args, kwargs))
            return {"id": "tp1"}

        bot.safe_amount = lambda symbol, qty: qty
        bot.safe_price = lambda symbol, price: price
        bot._build_protection_client_order_id = lambda *a, **k: "cid"
        bot._create_protection_order_with_retries = fake_create

        plan = type("Plan", (), {
            "symbol": "BTC/USDT:USDT",
            "side": "LONG",
            "entry_price": 100.0,
            "qty": 1.0,
        })()

        tp = type("TP", (), {
            "tp_name": "TP1",
            "side": "sell",
            "qty": 0.5,
            "price": 101.0,
            "reduce_only": True,
            "close_position": False,
        })()

        await bot._place_reduce_only_tp_from_plan(
            plan,
            tp,
            {"tp_order_type": "LIMIT"},
        )

        assert calls, "TP order was not created"

        args, kwargs = calls[0]
        params = args[5]

        assert params["reduceOnly"] is True
        assert "closePosition" not in params or params["closePosition"] is False

    asyncio.run(run())


def test_tp_market_order_uses_stop_price_and_quantity(monkeypatch):
    async def run():
        cls = emas.MainController
        bot = cls.__new__(cls)

        calls = []

        async def fake_create(*args, **kwargs):
            calls.append((args, kwargs))
            return {"id": "tp1"}

        bot.safe_amount = lambda symbol, qty: qty
        bot.safe_price = lambda symbol, price: price
        bot._build_protection_client_order_id = lambda *a, **k: "cid"
        bot._create_protection_order_with_retries = fake_create

        plan = type("Plan", (), {
            "symbol": "BTC/USDT:USDT",
            "side": "LONG",
            "entry_price": 100.0,
            "qty": 1.0,
        })()

        tp = type("TP", (), {
            "tp_name": "TP1",
            "side": "sell",
            "qty": 0.5,
            "price": 101.0,
            "reduce_only": True,
            "close_position": False,
        })()

        await bot._place_reduce_only_tp_from_plan(
            plan,
            tp,
            {"tp_order_type": "TAKE_PROFIT_MARKET"},
        )

        assert calls, "TP order was not created"

        args, kwargs = calls[0]
        params = args[5]

        assert args[1] == "take_profit_market"
        assert params["stopPrice"] == 101.0
        assert params["reduceOnly"] is True
        assert params["closePosition"] is False

    asyncio.run(run())

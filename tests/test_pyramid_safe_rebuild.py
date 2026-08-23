import asyncio

import pytest

from bot_runtime.pyramid_runtime_patch import _position_increased_after_exception
from bot_runtime.pyramid_safe_rebuild import (
    activate_adaptive_pyramid_rebuild,
    cancel_preserving_pyramid_sl,
    place_pyramid_protection_preserving_sl,
    reset_adaptive_pyramid_rebuild,
)


class FakeEngine:
    def __init__(self):
        self.orders = [
            {"id": "sl-old", "kind": "sl", "stopPrice": 100.0, "amount": 6.5},
            {"id": "tp-old", "kind": "tp", "price": 104.0, "amount": 1.0},
        ]
        self.live_pos = {
            "side": "long",
            "contracts": 8.0,
            "entryPrice": 100.2,
            "markPrice": 101.0,
        }
        self.replacements = []

    def _classify_protection_order(self, order):
        return order.get("kind")

    async def _collect_protection_orders(self, symbol):
        return [dict(order) for order in self.orders]

    async def _fetch_server_position_checked(self, symbol):
        return True, dict(self.live_pos)

    async def _replace_stop_loss_order(self, symbol, pos, stop, reason=""):
        self.replacements.append((symbol, pos["contracts"], stop, reason))
        self.orders = [order for order in self.orders if order.get("kind") != "sl"]
        order = {
            "id": "sl-full",
            "kind": "sl",
            "stopPrice": stop,
            "amount": pos["contracts"],
        }
        self.orders.append(order)
        return order

    def _position_signed_contracts(self, pos):
        return pos.get("contracts", 0.0)


def test_blanket_tp_refresh_preserves_existing_sl():
    engine = FakeEngine()
    seen = []

    async def original_cancel(self, symbol, reason="", orders=None):
        selected = list(orders or [])
        seen.extend(order["id"] for order in selected)
        selected_ids = {order["id"] for order in selected}
        self.orders = [order for order in self.orders if order["id"] not in selected_ids]
        return len(selected)

    token = activate_adaptive_pyramid_rebuild()
    try:
        cancelled = asyncio.run(
            cancel_preserving_pyramid_sl(
                engine,
                original_cancel,
                "ETH/USDT:USDT",
                reason="before new protection placement",
                orders=engine.orders,
            )
        )
    finally:
        reset_adaptive_pyramid_rebuild(token)

    assert cancelled == 1
    assert seen == ["tp-old"]
    assert any(order["id"] == "sl-old" for order in engine.orders)
    assert not any(order["id"] == "tp-old" for order in engine.orders)


def test_non_pyramid_cleanup_keeps_normal_all_order_behavior():
    engine = FakeEngine()
    seen = []

    async def original_cancel(self, symbol, reason="", orders=None):
        selected = list(orders or [])
        seen.extend(order["id"] for order in selected)
        return len(selected)

    cancelled = asyncio.run(
        cancel_preserving_pyramid_sl(
            engine,
            original_cancel,
            "ETH/USDT:USDT",
            reason="before new protection placement",
            orders=engine.orders,
        )
    )

    assert cancelled == 2
    assert set(seen) == {"sl-old", "tp-old"}


def test_pyramid_place_rebuilds_full_qty_sl_then_runs_tp_only():
    engine = FakeEngine()
    calls = []

    async def original_place(
        self,
        symbol,
        side,
        entry_price,
        qty,
        tp_distance=None,
        sl_distance=None,
        tp_qty_ratio=1.0,
        tp_targets=None,
        preserve_runner_qty=False,
    ):
        calls.append(
            {
                "symbol": symbol,
                "side": side,
                "entry": entry_price,
                "qty": qty,
                "sl_distance": sl_distance,
                "tp_targets": tp_targets,
                "preserve_runner_qty": preserve_runner_qty,
            }
        )
        return {"ok": True}

    token = activate_adaptive_pyramid_rebuild()
    try:
        result = asyncio.run(
            place_pyramid_protection_preserving_sl(
                engine,
                original_place,
                "ETH/USDT:USDT",
                "long",
                100.2,
                8.0,
                sl_distance=0.2,
                tp_targets=[{"label": "TP1", "distance": 3.8, "qty_ratio": 0.15}],
                preserve_runner_qty=True,
            )
        )
    finally:
        reset_adaptive_pyramid_rebuild(token)

    assert result == {"ok": True}
    assert engine.replacements
    assert engine.replacements[-1][1] == pytest.approx(8.0)
    assert engine.replacements[-1][2] == pytest.approx(100.0)
    assert calls[-1]["sl_distance"] is None
    assert calls[-1]["preserve_runner_qty"] is True
    assert any(
        order.get("kind") == "sl"
        and order.get("amount") == pytest.approx(8.0)
        and order.get("stopPrice") == pytest.approx(100.0)
        for order in engine.orders
    )


def test_post_fill_exception_detection_uses_exchange_position_growth():
    engine = FakeEngine()

    assert asyncio.run(
        _position_increased_after_exception(
            engine,
            "ETH/USDT:USDT",
            6.5,
        )
    ) is True

    engine.live_pos["contracts"] = 6.5
    assert asyncio.run(
        _position_increased_after_exception(
            engine,
            "ETH/USDT:USDT",
            6.5,
        )
    ) is False

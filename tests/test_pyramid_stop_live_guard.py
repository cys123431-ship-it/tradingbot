import asyncio
from types import SimpleNamespace

import pytest

from bot_runtime.pyramid_protection_live import enforce_adaptive_pyramid_live_sl_guard


class FakeStore:
    def __init__(self):
        self.record = SimpleNamespace(
            client_order_id="add-1",
            order_intent="POSITION_ADD",
            strategy="ADAPTIVE_TREND_PYRAMID",
            order_state="FILLED",
            created_at="2026-08-23T00:00:00Z",
        )
        self.transitions = []

    def active_for_symbol(self, symbol):
        return [self.record]

    def transition(self, client_order_id, state, **changes):
        self.transitions.append((client_order_id, state, changes))
        self.record.order_state = state
        return self.record


class FakeEngine:
    def __init__(self, *, avg_entry=100.8, mark=101.0, old_stop=100.0, old_qty=6.5):
        self.live_pos = {
            "side": "long",
            "contracts": 8.0,
            "entryPrice": avg_entry,
            "markPrice": mark,
        }
        self.state = {
            "strategy": "adaptive_breakout_trend_v1",
            "small_account_aggressive_active": True,
            "small_account_aggressive_cost_buffer_percent": 0.20,
            "adaptive_trend_initial_risk_distance": 2.0,
            "risk_distance": 2.0,
            "adaptive_trend_pyramid_add_count": 0,
            "last_stop_price": old_stop,
        }
        self.orders = [self._sl(old_stop, old_qty, "old-sl")] if old_stop else []
        self.cancelled = []
        self.replaced = []
        self.closed = []
        self.crypto_entry_lock_reason = "FILLED_TEST"
        self.trading_state_store = FakeStore()
        self.replace_behavior = None
        self.snapshot_calls = 0

    @staticmethod
    def _sl(stop, qty, order_id):
        return {
            "id": order_id,
            "kind": "sl",
            "side": "sell",
            "stopPrice": stop,
            "amount": qty,
        }

    def _position_signed_contracts(self, pos):
        return pos["contracts"]

    def _qty_matches_plan(self, expected, actual):
        return abs(float(expected) - float(actual)) <= max(1e-9, abs(float(expected)) * 0.001)

    async def _fetch_server_position_checked(self, symbol):
        return True, dict(self.live_pos)

    def _get_utbreakout_trailing_state(self, symbol):
        return self.state

    async def _collect_protection_orders_checked(self, symbol):
        self.snapshot_calls += 1
        return True, [dict(order) for order in self.orders]

    def _classify_protection_order(self, order):
        return order.get("kind")

    def _protection_trigger_price(self, order):
        return order.get("stopPrice")

    def _protection_order_amount(self, order):
        return order.get("amount")

    def _protection_order_side(self, order):
        return order.get("side")

    def _protection_order_id(self, order):
        return (order or {}).get("id")

    async def _cancel_protection_orders_by_kind(self, symbol, kinds, reason=""):
        self.cancelled.append((symbol, set(kinds), reason))
        self.orders = [order for order in self.orders if order.get("kind") not in set(kinds)]
        return 1

    async def _replace_stop_loss_order(self, symbol, pos, stop, reason=""):
        self.replaced.append((stop, pos["contracts"], reason))
        if self.replace_behavior is not None:
            return await self.replace_behavior(stop)
        self.orders = [order for order in self.orders if order.get("kind") != "sl"]
        order = self._sl(stop, pos["contracts"], f"new-{len(self.replaced)}")
        self.orders.append(order)
        return order

    async def _emergency_close_position_without_stop_loss(self, symbol, reason="", max_attempts=5):
        self.closed.append((symbol, reason, max_attempts))
        return {"status": "EMERGENCY_CLOSED", "closed": True}

    def _set_utbreakout_trailing_state(self, symbol, state):
        self.state = state

    def _set_crypto_entry_lock(self, reason):
        self.crypto_entry_lock_reason = reason


def run_guard(engine, *, before_stop=100.0, result=None):
    return asyncio.run(
        enforce_adaptive_pyramid_live_sl_guard(
            engine,
            "ETH/USDT:USDT",
            before_qty=6.5,
            before_stop=before_stop,
            before_add_count=0,
            result=result or {"status": "ADDED"},
            cfg={
                "adaptive_trend_stop_verify_attempts": 3,
                "adaptive_trend_stop_verify_delay_sec": 0.0,
            },
        )
    )


def test_same_price_old_qty_is_replaced_with_full_position_qty():
    engine = FakeEngine(avg_entry=100.8, mark=101.0, old_stop=100.0, old_qty=6.5)

    result = run_guard(engine)

    assert result["status"] == "ADDED"
    assert result["exchange_stop_verified"] is True
    assert result["verified_stop"] == pytest.approx(100.0)
    assert engine.cancelled
    assert engine.orders[-1]["amount"] == pytest.approx(8.0)
    assert not engine.closed


def test_fee_target_failure_falls_back_to_live_pre_add_stop_before_closing():
    engine = FakeEngine(avg_entry=100.2, mark=101.0, old_stop=100.0, old_qty=6.5)
    calls = 0

    async def behavior(stop):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        engine.orders = [engine._sl(stop, 8.0, "fallback-full")]
        return engine.orders[0]

    engine.replace_behavior = behavior

    result = run_guard(engine)

    assert result["status"] == "ADDED"
    assert result["verified_stop"] == pytest.approx(100.0)
    assert calls >= 2
    assert engine.orders[0]["amount"] == pytest.approx(8.0)
    assert not engine.closed


def test_exchange_visibility_delay_does_not_trigger_immediate_close():
    engine = FakeEngine(avg_entry=100.2, mark=101.0, old_stop=100.0, old_qty=6.5)
    pending = {"stop": None, "polls": 0}

    async def behavior(stop):
        pending["stop"] = stop
        return {"id": "accepted-but-not-visible-yet"}

    original_collect = engine._collect_protection_orders_checked

    async def delayed_collect(symbol):
        if pending["stop"] is None:
            return await original_collect(symbol)
        pending["polls"] += 1
        if pending["polls"] < 2:
            return True, [engine._sl(100.0, 6.5, "old-sl")]
        engine.orders = [engine._sl(pending["stop"], 8.0, "visible-new-sl")]
        return True, [dict(engine.orders[0])]

    engine.replace_behavior = behavior
    engine._collect_protection_orders_checked = delayed_collect

    result = run_guard(engine)

    assert result["status"] == "ADDED"
    assert result["exchange_stop_verified"] is True
    assert pending["polls"] >= 2
    assert not engine.closed


def test_stale_cached_stop_above_mark_is_not_treated_as_real_exchange_sl():
    engine = FakeEngine(avg_entry=100.8, mark=101.0, old_stop=100.0, old_qty=8.0)
    engine.state["last_stop_price"] = 101.2

    result = run_guard(engine, before_stop=101.2)

    assert result["status"] == "ADDED"
    assert result["verified_stop"] == pytest.approx(100.0)
    assert not engine.closed


def test_geometry_failure_after_fill_repairs_exchange_sl_without_liquidation():
    engine = FakeEngine(avg_entry=100.2, mark=101.0, old_stop=100.5, old_qty=6.5)

    result = run_guard(
        engine,
        before_stop=100.5,
        result={
            "status": "BLOCKED",
            "reason": "adaptive trend average entry/SL geometry invalid",
        },
    )

    assert result["status"] == "ADDED_PROTECTION_REPAIRED"
    assert result["verified_stop"] == pytest.approx(100.5)
    assert engine.orders[-1]["amount"] == pytest.approx(8.0)
    assert not engine.closed


def test_only_closes_after_exchange_sl_repair_and_verification_both_fail():
    engine = FakeEngine(avg_entry=100.2, mark=101.0, old_stop=None, old_qty=0.0)

    async def behavior(stop):
        return None

    engine.replace_behavior = behavior

    result = run_guard(engine, before_stop=None)

    assert result["status"] == "EMERGENCY_CLOSED"
    assert engine.closed

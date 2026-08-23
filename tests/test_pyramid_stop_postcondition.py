import asyncio
from types import SimpleNamespace

import pytest

from bot_runtime.pyramid_protection import enforce_adaptive_pyramid_stop_postcondition


class FakeStore:
    def __init__(self):
        self.records = [
            SimpleNamespace(
                client_order_id="adaptive-add-1",
                order_intent="POSITION_ADD",
                strategy="ADAPTIVE_TREND_PYRAMID",
                order_state="FILLED",
                created_at="2026-08-23T00:00:00Z",
            )
        ]
        self.transitions = []

    def active_for_symbol(self, symbol):
        return self.records

    def transition(self, client_order_id, state, **changes):
        self.transitions.append((client_order_id, state, changes))
        self.records[0].order_state = state
        return self.records[0]


class FakeEngine:
    def __init__(self, *, stop=100.5, replacement_ok=True, post_stop=None):
        self.state = {
            "strategy": "adaptive_breakout_trend_v1",
            "small_account_aggressive_active": True,
            "small_account_aggressive_cost_buffer_percent": 0.20,
            "adaptive_trend_pyramid_add_count": 0,
            "last_stop_price": stop,
        }
        self.live_pos = {
            "side": "long",
            "contracts": 8.0,
            "entryPrice": 100.2,
            "markPrice": 101.0,
        }
        self.audit_calls = 0
        self.cancelled = []
        self.replacements = []
        self.closed = []
        self.lock = None
        self.crypto_entry_lock_reason = "FILLED_TEST"
        self.trading_state_store = FakeStore()
        self.replacement_ok = replacement_ok
        self.post_stop = post_stop

    def _position_signed_contracts(self, pos):
        return pos["contracts"]

    async def _fetch_server_position_checked(self, symbol):
        return True, self.live_pos

    def _get_utbreakout_trailing_state(self, symbol):
        return self.state

    async def _audit_protection_orders(self, *args, **kwargs):
        self.audit_calls += 1
        return {
            "fetch_ok": True,
            "sl_present": True,
            "sl_qty_mismatch": self.audit_calls == 1,
        }

    async def _cancel_protection_orders_by_kind(self, symbol, kinds, reason=""):
        self.cancelled.append((symbol, kinds, reason))
        return 1

    async def _replace_stop_loss_order(self, symbol, pos, stop, reason=""):
        self.replacements.append((symbol, pos["contracts"], stop, reason))
        return {"id": "sl-new"} if self.replacement_ok else None

    async def _current_stop_loss_price(self, symbol, state):
        if self.post_stop is not None:
            return self.post_stop
        return self.replacements[-1][2]

    async def _emergency_close_position_without_stop_loss(
        self,
        symbol,
        reason="",
        max_attempts=5,
    ):
        self.closed.append((symbol, reason, max_attempts))
        return {"status": "EMERGENCY_CLOSED", "closed": True}

    def _set_utbreakout_trailing_state(self, symbol, state):
        self.state = state

    def _set_crypto_entry_lock(self, reason):
        self.lock = reason
        self.crypto_entry_lock_reason = reason

    def _protection_order_id(self, order):
        return order.get("id")


def test_profit_side_pre_add_stop_repairs_geometry_failure_without_loosening():
    engine = FakeEngine(stop=100.5)

    result = asyncio.run(
        enforce_adaptive_pyramid_stop_postcondition(
            engine,
            "BTC/USDT:USDT",
            before_qty=6.5,
            before_stop=100.5,
            before_add_count=0,
            result={
                "status": "BLOCKED",
                "reason": "adaptive trend average entry/SL geometry invalid",
            },
            cfg={},
        )
    )

    assert result["status"] == "ADDED_PROTECTION_REPAIRED"
    assert result["required_stop"] == pytest.approx(100.5)
    assert engine.replacements[-1][1] == pytest.approx(8.0)
    assert engine.cancelled
    assert engine.state["last_stop_price"] == pytest.approx(100.5)
    assert engine.state["adaptive_trend_pyramid_add_count"] == 1
    assert engine.trading_state_store.transitions[-1][1] == "PROTECTED"
    assert engine.lock is None


def test_failed_fee_aware_stop_replacement_emergency_closes_added_position():
    engine = FakeEngine(stop=100.0, replacement_ok=False)

    result = asyncio.run(
        enforce_adaptive_pyramid_stop_postcondition(
            engine,
            "BTC/USDT:USDT",
            before_qty=6.5,
            before_stop=100.0,
            before_add_count=0,
            result={"status": "ADDED"},
            cfg={},
        )
    )

    assert result["status"] == "EMERGENCY_CLOSED"
    assert engine.closed


def test_sl_price_mismatch_is_not_accepted_just_because_sl_exists():
    engine = FakeEngine(stop=100.0, replacement_ok=True, post_stop=100.0)

    result = asyncio.run(
        enforce_adaptive_pyramid_stop_postcondition(
            engine,
            "BTC/USDT:USDT",
            before_qty=6.5,
            before_stop=100.0,
            before_add_count=0,
            result={"status": "ADDED"},
            cfg={},
        )
    )

    assert result["status"] == "EMERGENCY_CLOSED"
    assert engine.closed


def test_required_profit_lock_already_crossed_emergency_closes():
    engine = FakeEngine(stop=101.2)
    engine.live_pos["markPrice"] = 101.0

    result = asyncio.run(
        enforce_adaptive_pyramid_stop_postcondition(
            engine,
            "BTC/USDT:USDT",
            before_qty=6.5,
            before_stop=101.2,
            before_add_count=0,
            result={"status": "ADDED"},
            cfg={},
        )
    )

    assert result["status"] == "EMERGENCY_CLOSED"
    assert engine.closed


def test_signal_engine_runtime_guard_is_installed():
    from bot_runtime.signal_engine import SignalEngine

    method = SignalEngine._maybe_apply_adaptive_trend_pyramiding
    assert SignalEngine._adaptive_pyramid_stop_guard_installed is True
    assert getattr(method, "__runtime_original__", None) is not None

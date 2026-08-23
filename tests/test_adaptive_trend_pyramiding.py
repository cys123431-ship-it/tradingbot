from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bot_runtime.signal_exit as signal_exit_module
from bot_runtime.signal_exit import SignalExitMixin
from utbreakout.adaptive_breakout_trend import ADAPTIVE_BREAKOUT_TREND_STRATEGY


@pytest.mark.parametrize(
    ("side", "mark_price", "average_entry", "expected_stop"),
    (
        ("long", 101.0, 100.2, 100.2 * 1.0012),
        ("short", 99.0, 99.8, 99.8 * 0.9988),
        ("long", 101.0, 100.8, 100.0),
    ),
)
def test_adaptive_trend_adds_only_at_profitable_stage_and_reprotects(
    monkeypatch,
    side,
    mark_price,
    average_entry,
    expected_stop,
):
    engine = SignalExitMixin()
    state = {
        "strategy": ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        "side": side,
        "entry_price": 100.0,
        "risk_distance": 2.0,
        "initial_qty": 6.5,
        "leverage": 10,
        "last_stop_price": 100.0,
        "last_bar_ts": 1_700_000_000,
        "adaptive_trend_pyramid_enabled": True,
        "adaptive_trend_pyramid_trigger_r": (0.50, 1.00, 1.75),
        "adaptive_trend_pyramid_target_fractions": (0.80, 0.90, 1.00),
        "adaptive_trend_pyramid_add_count": 0,
        "adaptive_trend_target_qty": 10.0,
        "adaptive_trend_initial_entry_price": 100.0,
        "adaptive_trend_initial_risk_distance": 2.0,
        "adaptive_trend_partial_r_multiple": 2.0,
        "adaptive_trend_partial_ratio": 0.15,
        "trailing_atr_multiplier": 3.8,
        "planned_tp_orders": [{"qty": 1.0}],
    }
    calls = {
        "add_qty": None,
        "protection": None,
        "notices": [],
        "stop_replacements": [],
    }

    class Execution:
        async def submit_position_add(self, **kwargs):
            calls["add_qty"] = kwargs["qty"]
            return SimpleNamespace(
                accepted=True,
                state="FILLED",
                error=None,
                client_order_id="adaptive-add-1",
                order={"id": "adaptive-add-1"},
                position={
                    "side": side,
                    "contracts": 8.0,
                    "entryPrice": average_entry,
                },
            )

    async def audit(*args, **kwargs):
        return {
            "fetch_ok": True,
            "sl_present": True,
            "sl_qty_mismatch": False,
        }

    async def place_protection(*args, **kwargs):
        calls["protection"] = {"args": args, "kwargs": kwargs}

    def register(symbol, registered_side, entry, qty, plan, cfg):
        state.update(plan)
        state["entry_price"] = entry
        state["initial_qty"] = qty
        state["planned_tp_orders"] = [{"qty": qty * 0.15}]
        return state

    engine.is_upbit_mode = lambda: False
    engine._get_utbreakout_trailing_state = lambda symbol: state
    engine._position_signed_contracts = lambda pos: pos.get("contracts", 0.0)
    engine.get_balance_info = lambda: asyncio.sleep(
        0,
        result=(5_000.0, 5_000.0, 0.0),
    )
    engine.safe_amount = lambda symbol, qty: qty
    engine._current_stop_loss_price = lambda symbol, current: asyncio.sleep(
        0,
        result=100.0,
    )
    engine._replace_stop_loss_order = lambda symbol, pos, stop, **kwargs: asyncio.sleep(
        0,
        result=(
            calls["stop_replacements"].append(stop)
            or {"id": "combined-be-stop"}
        ),
    )
    engine._planned_tp_orders_from_state = lambda symbol, current: list(
        current.get("planned_tp_orders") or []
    )
    engine._audit_protection_orders = audit
    engine.ensure_market_settings = lambda *args, **kwargs: asyncio.sleep(0)
    engine._preflight_liquidation_safety = lambda *args, **kwargs: asyncio.sleep(
        0,
        result={"valid": True},
    )
    engine.crypto_execution = Execution()
    engine.position_cache = None
    engine.position_cache_time = 0
    engine._fetch_position_with_liquidation = lambda symbol, pos: asyncio.sleep(
        0,
        result=(True, pos),
    )
    engine._verify_actual_liquidation_safety = lambda *args, **kwargs: asyncio.sleep(
        0,
        result={
            "valid": True,
            "position": {
                "side": side,
                "contracts": 8.0,
                "entryPrice": average_entry,
            },
        },
    )
    engine._place_tp_sl_orders = place_protection
    engine._set_crypto_entry_lock = lambda reason: None
    engine._register_utbreakout_trailing_state = register
    engine.ctrl = SimpleNamespace(
        format_symbol_for_display=lambda symbol: symbol,
        notify=lambda message: asyncio.sleep(
            0,
            result=calls["notices"].append(message),
        ),
    )

    monkeypatch.setattr(signal_exit_module, "time", time, raising=False)
    monkeypatch.setattr(
        signal_exit_module,
        "ADAPTIVE_BREAKOUT_TREND_STRATEGY",
        ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        raising=False,
    )
    monkeypatch.setattr(
        signal_exit_module,
        "_safe_float_or_none",
        lambda value: float(value) if value not in (None, "") else None,
        raising=False,
    )
    monkeypatch.setattr(
        signal_exit_module,
        "_crypto_entry_block_reason",
        lambda *args, **kwargs: "PROTECTED:TEST/USDT:USDT:parent",
        raising=False,
    )
    monkeypatch.setattr(
        signal_exit_module,
        "_ensure_trading_safety_runtime",
        lambda engine: None,
        raising=False,
    )
    monkeypatch.setattr(
        signal_exit_module,
        "_mark_crypto_entry_state",
        lambda *args, **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        signal_exit_module,
        "OrderState",
        SimpleNamespace(PROTECTED="PROTECTED"),
        raising=False,
    )

    bound_method = engine._maybe_apply_adaptive_trend_pyramiding
    pyramid_method = getattr(bound_method, "__runtime_original__", None)
    call_args = (engine,) if pyramid_method is not None else ()
    pyramid_method = pyramid_method or bound_method
    result = asyncio.run(
        pyramid_method(
            *call_args,
            "TEST/USDT:USDT",
            {
                "side": side,
                "contracts": 6.5,
                "entryPrice": 100.0,
                "markPrice": mark_price,
            },
            None,
            {},
        )
    )

    assert result["status"] == "ADDED"
    assert calls["add_qty"] == pytest.approx(1.5)
    assert calls["protection"] is not None
    assert calls["protection"]["kwargs"]["preserve_runner_qty"] is True
    assert calls["protection"]["kwargs"]["sl_distance"] == pytest.approx(
        abs(average_entry - 100.0)
    )
    if expected_stop == 100.0:
        assert calls["stop_replacements"] == []
    else:
        assert calls["stop_replacements"] == [pytest.approx(expected_stop)]
    assert state["last_stop_price"] == pytest.approx(expected_stop)
    assert state["risk_distance"] == pytest.approx(2.0)
    if expected_stop != 100.0:
        assert (
            state["last_stop_price"] > average_entry
            if side == "long"
            else state["last_stop_price"] < average_entry
        )
    assert state["adaptive_trend_pyramid_add_count"] == 1
    assert state["adaptive_trend_initial_entry_price"] == pytest.approx(100.0)
    assert calls["notices"]


def test_adaptive_trend_does_not_add_before_profit_trigger(monkeypatch):
    engine = SignalExitMixin()
    state = {
        "strategy": ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        "side": "long",
        "entry_price": 100.0,
        "risk_distance": 2.0,
        "adaptive_trend_pyramid_enabled": True,
        "adaptive_trend_pyramid_trigger_r": (0.50,),
        "adaptive_trend_pyramid_target_fractions": (1.00,),
        "adaptive_trend_target_qty": 10.0,
    }
    engine.is_upbit_mode = lambda: False
    engine._get_utbreakout_trailing_state = lambda symbol: state
    engine._position_signed_contracts = lambda pos: pos.get("contracts", 0.0)
    monkeypatch.setattr(
        signal_exit_module,
        "ADAPTIVE_BREAKOUT_TREND_STRATEGY",
        ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        raising=False,
    )
    monkeypatch.setattr(
        signal_exit_module,
        "_safe_float_or_none",
        lambda value: float(value) if value not in (None, "") else None,
        raising=False,
    )

    bound_method = engine._maybe_apply_adaptive_trend_pyramiding
    pyramid_method = getattr(bound_method, "__runtime_original__", None)
    call_args = (engine,) if pyramid_method is not None else ()
    pyramid_method = pyramid_method or bound_method
    result = asyncio.run(
        pyramid_method(
            *call_args,
            "TEST/USDT:USDT",
            {
                "side": "long",
                "contracts": 6.5,
                "entryPrice": 100.0,
                "markPrice": 100.5,
            },
            None,
            {},
        )
    )

    assert result["status"] == "WAITING"
    assert result["pnl_r"] == pytest.approx(0.25)


def test_adaptive_trend_marks_subminimum_target_remainder_complete(monkeypatch):
    engine = SignalExitMixin()
    state = {
        "strategy": ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        "side": "long",
        "entry_price": 100.0,
        "risk_distance": 2.0,
        "adaptive_trend_pyramid_enabled": True,
        "adaptive_trend_pyramid_trigger_r": (0.50,),
        "adaptive_trend_pyramid_target_fractions": (1.00,),
        "adaptive_trend_pyramid_add_count": 0,
        "adaptive_trend_target_qty": 10.005,
    }
    safe_amount_calls = []
    engine.is_upbit_mode = lambda: False
    engine._get_utbreakout_trailing_state = lambda symbol: state
    engine._position_signed_contracts = lambda pos: pos.get("contracts", 0.0)
    engine._get_min_amount_for_symbol = lambda symbol: 0.01
    engine.safe_amount = lambda symbol, qty: safe_amount_calls.append(qty) or qty
    engine._set_utbreakout_trailing_state = lambda symbol, value: None
    monkeypatch.setattr(signal_exit_module, "datetime", datetime, raising=False)
    monkeypatch.setattr(signal_exit_module, "timezone", timezone, raising=False)
    monkeypatch.setattr(
        signal_exit_module,
        "ADAPTIVE_BREAKOUT_TREND_STRATEGY",
        ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        raising=False,
    )
    monkeypatch.setattr(
        signal_exit_module,
        "_safe_float_or_none",
        lambda value: float(value) if value not in (None, "") else None,
        raising=False,
    )

    bound_method = engine._maybe_apply_adaptive_trend_pyramiding
    pyramid_method = getattr(bound_method, "__runtime_original__", None)
    call_args = (engine,) if pyramid_method is not None else ()
    pyramid_method = pyramid_method or bound_method
    result = asyncio.run(
        pyramid_method(
            *call_args,
            "TEST/USDT:USDT",
            {
                "side": "long",
                "contracts": 10.0,
                "entryPrice": 100.0,
                "markPrice": 101.0,
            },
            None,
            {},
        )
    )

    assert result["status"] == "COMPLETE"
    assert result["residual_qty"] == pytest.approx(0.005)
    assert state["adaptive_trend_pyramid_add_count"] == 1
    assert safe_amount_calls == []


def test_small_account_adaptive_trend_add_ignores_daily_pnl_but_keeps_trade_cap(monkeypatch):
    engine = SignalExitMixin()
    state = {
        "strategy": ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        "side": "long",
        "entry_price": 100.0,
        "risk_distance": 2.0,
        "leverage": 5,
        "adaptive_trend_pyramid_enabled": True,
        "adaptive_trend_pyramid_trigger_r": (0.50,),
        "adaptive_trend_pyramid_target_fractions": (0.80,),
        "adaptive_trend_pyramid_add_count": 0,
        "adaptive_trend_target_qty": 10.0,
        "adaptive_trend_initial_entry_price": 100.0,
        "adaptive_trend_initial_risk_distance": 2.0,
        "small_account_aggressive_active": True,
        "small_account_aggressive_max_loss_usdt": 1.0,
        "small_account_aggressive_daily_loss_limit_usdt": 35.0,
        "small_account_aggressive_cost_buffer_percent": 0.20,
    }
    engine.is_upbit_mode = lambda: False
    engine._get_utbreakout_trailing_state = lambda symbol: state
    engine._position_signed_contracts = lambda pos: pos.get("contracts", 0.0)
    engine.get_balance_info = lambda: asyncio.sleep(0, result=(100.0, 100.0, 0.0))
    engine.safe_amount = lambda symbol, qty: qty
    engine._current_stop_loss_price = lambda symbol, current: asyncio.sleep(
        0,
        result=100.0,
    )
    engine.db = SimpleNamespace(get_daily_stats=lambda: (12, -500.0))

    monkeypatch.setattr(
        signal_exit_module,
        "ADAPTIVE_BREAKOUT_TREND_STRATEGY",
        ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        raising=False,
    )
    monkeypatch.setattr(
        signal_exit_module,
        "_safe_float_or_none",
        lambda value: float(value) if value not in (None, "") else None,
        raising=False,
    )

    bound_method = engine._maybe_apply_adaptive_trend_pyramiding
    pyramid_method = getattr(bound_method, "__runtime_original__", None)
    call_args = (engine,) if pyramid_method is not None else ()
    pyramid_method = pyramid_method or bound_method
    result = asyncio.run(
        pyramid_method(
            *call_args,
            "TEST/USDT:USDT",
            {
                "side": "long",
                "contracts": 6.5,
                "entryPrice": 100.0,
                "markPrice": 101.0,
            },
            None,
            {},
        )
    )

    assert result["status"] == "BLOCKED"
    assert "exceeds remaining cap" in result["reason"]
    assert result["projected_loss_usdt"] > 1.0

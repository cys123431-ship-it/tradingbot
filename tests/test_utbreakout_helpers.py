from datetime import datetime, timezone
import asyncio

import pytest

from utbreakout.indicators import previous_donchian
from utbreakout.research import summarize_diagnostic_events
from utbreakout.risk import calculate_risk_plan


def _signal_engine_cls():
    return pytest.importorskip("emas", reason="emas runtime dependencies are optional in CI").SignalEngine


def test_previous_donchian_excludes_current_candle():
    highs = [10, 11, 12, 13, 14, 999]
    lows = [9, 8, 7, 6, 5, -999]

    result = previous_donchian(highs, lows, 5)

    assert result["ready"] is True
    assert result["high"] == 14
    assert result["low"] == 5


def test_risk_plan_uses_loss_budget_not_fixed_margin():
    plan = calculate_risk_plan(
        side="long",
        entry_price=100.0,
        atr_value=2.0,
        stop_atr_multiplier=1.5,
        ut_stop=96.0,
        take_profit_r_multiple=2.0,
        min_risk_reward=2.0,
        balance_usdt=4000.0,
        risk_per_trade_percent=1.0,
        max_risk_per_trade_usdt=50.0,
        leverage=10.0,
    )

    assert plan["risk_distance"] == 4.0
    assert plan["risk_usdt"] == 40.0
    assert plan["qty"] == 10.0
    assert plan["planned_notional"] == 1000.0
    assert plan["planned_margin"] == 100.0
    assert plan["take_profit"] == 108.0


def test_research_summary_detects_set_concentration_and_protection_gaps():
    events = []
    for idx in range(6):
        events.append({
            "ts": datetime(2026, 1, 1, 0, idx, tzinfo=timezone.utc).isoformat(),
            "event": "rejected",
            "symbol": "BTC/USDT",
            "side": "long",
            "code": "REJECTED_ADX_LOW",
            "auto_selected_set_id": 2,
            "candidate_type": "bias_state",
            "decision_candle_ts": idx,
            "risk_usdt": 1.0,
            "risk_distance": 10.0,
            "entry_price": 1000.0,
            "planned_margin": 5.0,
            "planned_notional": 50.0,
        })
    events.append({
        "ts": datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc).isoformat(),
        "event": "accepted",
        "symbol": "BTC/USDT",
        "side": "short",
        "code": "ACCEPTED_ENTRY",
        "auto_selected_set_id": 7,
        "candidate_type": "fresh_signal",
        "decision_candle_ts": 7,
    })

    summary = summarize_diagnostic_events(
        events,
        protection_status={"BTC/USDT": {"missing_sl": True, "missing_tp": False}},
    )

    assert summary["top_set"] == "Set2"
    assert summary["top_set_share_pct"] > 50.0
    assert summary["top_rejects"][0] == ("REJECTED_ADX_LOW", 6)
    assert summary["protection_missing_sl"] == ["BTC/USDT"]


def test_protection_order_classifies_binance_stop_market_from_orig_type():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    order = {
        "type": "market",
        "side": "sell",
        "info": {
            "type": "STOP_MARKET",
            "origType": "STOP_MARKET",
            "stopPrice": "78000",
            "reduceOnly": "true",
            "symbol": "BTCUSDT",
        },
    }

    assert signal_engine._classify_protection_order(engine, order) == "sl"
    assert signal_engine._protection_order_matches_symbol(engine, order, "BTC/USDT") is True


def test_protection_order_keeps_take_profit_separate_from_stop_loss():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    take_profit_market = {
        "type": "market",
        "side": "sell",
        "info": {
            "type": "TAKE_PROFIT_MARKET",
            "origType": "TAKE_PROFIT_MARKET",
            "stopPrice": "82000",
            "reduceOnly": "true",
        },
    }
    take_profit_limit = {"type": "limit", "side": "sell", "reduceOnly": True}

    assert signal_engine._classify_protection_order(engine, take_profit_market) == "tp"
    assert signal_engine._classify_protection_order(engine, take_profit_limit) == "tp"


class _DummyCtrl:
    def format_symbol_for_display(self, symbol):
        return symbol

    async def notify(self, message):
        self.last_message = message


class _FakeExchange:
    def __init__(self, orders, symbol_scope_returns=True):
        self.orders = list(orders)
        self.cancelled = []
        self.symbol_scope_returns = symbol_scope_returns

    def fetch_open_orders(self, symbol=None):
        if symbol and not self.symbol_scope_returns:
            return []
        return list(self.orders)

    def cancel_order(self, order_id, symbol):
        self.cancelled.append((str(order_id), symbol))
        self.orders = [
            order for order in self.orders
            if str(order.get("id") or order.get("info", {}).get("orderId")) != str(order_id)
        ]
        return {"id": order_id}


def _protection_engine(orders, symbol_scope_returns=True):
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.exchange = _FakeExchange(orders, symbol_scope_returns=symbol_scope_returns)
    engine.ctrl = _DummyCtrl()
    engine.last_protection_alert_ts = {}
    engine.last_protection_order_status = {}
    engine.is_upbit_mode = lambda: False
    return engine


def test_protection_audit_cancels_orphan_orders_even_when_symbol_fetch_misses_them():
    engine = _protection_engine(
        [
            {
                "id": "sl-old",
                "side": "buy",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "105",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        symbol_scope_returns=False,
    )

    status = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=None, expected_tp=False, expected_sl=False, alert=False)
    )

    assert status["status"] == "ORPHAN_CANCELLED"
    assert status["orphan_cancelled"] == 1
    assert engine.exchange.orders == []


def test_protection_audit_deduplicates_short_stop_loss_orders():
    orders = [
        {
            "id": "sl-old",
            "side": "buy",
            "type": "market",
            "timestamp": 1000,
            "info": {"origType": "STOP_MARKET", "stopPrice": "105", "reduceOnly": "true", "symbol": "BTCUSDT"},
        },
        {
            "id": "sl-new",
            "side": "buy",
            "type": "market",
            "timestamp": 2000,
            "info": {"origType": "STOP_MARKET", "stopPrice": "106", "reduceOnly": "true", "symbol": "BTCUSDT"},
        },
        {
            "id": "tp",
            "side": "buy",
            "type": "limit",
            "price": "90",
            "reduceOnly": True,
            "info": {"symbol": "BTCUSDT"},
        },
    ]
    engine = _protection_engine(orders)
    pos = {"side": "short", "contracts": 1, "entryPrice": 100}

    status = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=pos, expected_tp=True, expected_sl=True, alert=False)
    )

    assert status["status"] == "DUPLICATE_CANCELLED"
    assert status["duplicate_cancelled"] == 1
    remaining_ids = {order["id"] for order in engine.exchange.orders}
    assert remaining_ids == {"sl-new", "tp"}

from __future__ import annotations

from trading_safety.profile_rollout import (
    get_tradfi_profile_rollout_state,
    update_tradfi_profile_rollout,
)
from trading_safety.order_state import SQLiteTradingStateStore
from trading_safety.reconciliation import ReconciliationResult
from utbreakout.adaptive_breakout_trend import AdaptiveBreakoutTrendDecision
from utbreakout.tradfi_pattern_profile import (
    TRADFI_PATTERN_PROFILE_VERSION,
    evaluate_tradfi_pattern_profile,
    normalize_tradfi_pattern_profile_config,
)


def _trend_rows(count=90, step=0.18, *, breakout=False):
    rows = []
    price = 100.0
    for index in range(count):
        open_price = price
        price += step + (0.02 if index % 3 == 0 else -0.01)
        rows.append({
            "timestamp": index * 3_600_000,
            "open": open_price,
            "high": max(open_price, price) + 0.10,
            "low": min(open_price, price) - 0.10,
            "close": price,
            "volume": 1_000.0,
        })
    if breakout:
        prior_high = max(row["high"] for row in rows[-25:-1])
        rows[-1].update({
            "open": rows[-2]["close"],
            "close": prior_high + 0.60,
            "high": prior_high + 0.70,
            "low": rows[-2]["close"] - 0.05,
            "volume": 1_500.0,
        })
    return rows


def _soft_long_wait():
    return AdaptiveBreakoutTrendDecision(
        allowed=False,
        side="long",
        reason="waiting_for_weighted_trend_entry",
        metrics={
            "ema_aligned": True,
            "weighted_momentum": 0.32,
            "reference_price": 116.0,
            "atr": 0.50,
            "structure_stop": 114.0,
            "signal_candle_ts": 123,
            "target_risk_percent": 1.75,
        },
    )


def test_trend_aligned_chart_breakout_adds_tradfi_or_entry():
    decision = evaluate_tradfi_pattern_profile(
        _trend_rows(breakout=True),
        _soft_long_wait(),
        higher_timeframe_rows=_trend_rows(100, 0.25),
        daily_rows=_trend_rows(100, 0.35),
        benchmark_directions={"SPY": "long", "QQQ": "long"},
        session_status={"open": True, "reason": "regular_session_open"},
    )

    assert decision.allowed is True
    assert decision.side == "long"
    assert decision.metrics["tradfi_entry_mode"] == "pattern_or_entry"
    assert decision.metrics["tradfi_pattern_entry_allowed"] is True
    assert "breakout" in decision.reason.lower()


def test_pattern_or_entry_requires_regular_session_but_base_entry_does_not():
    closed = {"open": False, "reason": "outside_regular_session"}
    pattern_only = evaluate_tradfi_pattern_profile(
        _trend_rows(breakout=True),
        _soft_long_wait(),
        higher_timeframe_rows=_trend_rows(100, 0.25),
        daily_rows=_trend_rows(100, 0.35),
        benchmark_directions={"SPY": "long"},
        session_status=closed,
    )
    base_allowed = AdaptiveBreakoutTrendDecision(
        allowed=True,
        side="long",
        score=72.0,
        risk_multiplier=1.0,
        reason="base adaptive entry",
        metrics=dict(_soft_long_wait().metrics),
    )
    retained = evaluate_tradfi_pattern_profile(
        _trend_rows(breakout=True),
        base_allowed,
        higher_timeframe_rows=_trend_rows(100, 0.25),
        daily_rows=_trend_rows(100, 0.35),
        session_status=closed,
    )

    assert pattern_only.allowed is False
    assert retained.allowed is True
    assert retained.metrics["tradfi_entry_mode"] == "base_adaptive_trend"


def test_tradfi_profile_never_exceeds_exchange_ten_x_cap():
    config = normalize_tradfi_pattern_profile_config({
        "maximum_leverage": 50,
        "leverage_steps": [5, 8, 10, 15, 50],
    })

    assert config["maximum_leverage"] == 10
    assert config["leverage_steps"] == (5, 8, 10)


def _snapshot(*, positions=(), orders=()):
    return ReconciliationResult(
        safe_to_trade=True,
        snapshot_complete=True,
        positions_ok=True,
        regular_orders_ok=True,
        algo_orders_ok=True,
        positions=list(positions),
        open_orders=list(orders),
    )


def test_rollout_preserves_deployment_position_until_position_and_orders_clear(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    position = {"symbol": "SNDK/USDT:USDT", "contracts": 0.01}
    stop = {"symbol": "SNDK/USDT:USDT", "type": "STOP_MARKET"}

    pending = update_tradfi_profile_rollout(
        store,
        _snapshot(positions=[position], orders=[stop]),
    )
    order_pending = update_tradfi_profile_rollout(
        store,
        _snapshot(orders=[stop]),
    )
    active = update_tradfi_profile_rollout(store, _snapshot())

    assert pending["state"] == "pending_existing_position"
    assert pending["pending_symbols"] == ["SNDKUSDT"]
    assert order_pending["active"] is False
    assert order_pending["remaining_orders"] == ["SNDKUSDT"]
    assert active["active"] is True
    assert active["version"] == TRADFI_PATTERN_PROFILE_VERSION
    assert get_tradfi_profile_rollout_state(store)["active"] is True


def test_rollout_activates_immediately_when_deployment_snapshot_is_flat(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")

    state = update_tradfi_profile_rollout(store, _snapshot())

    assert state["active"] is True
    assert state["state"] == "active"


def test_rollout_waits_when_only_residual_protection_exists_at_deployment(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    stop = {
        "symbol": "SNDK/USDT:USDT",
        "type": "STOP_MARKET",
        "reduceOnly": True,
    }

    pending = update_tradfi_profile_rollout(store, _snapshot(orders=[stop]))
    active = update_tradfi_profile_rollout(store, _snapshot())

    assert pending["active"] is False
    assert pending["pending_symbols"] == ["SNDKUSDT"]
    assert active["active"] is True

from types import SimpleNamespace

from bot_runtime.desktop_monitor import build_desktop_monitor_snapshot
from bot_runtime.entry_reason_ko import explain_entry_reason_ko
from scripts.desktop_monitor_stream import classify_position, normalize_order, normalize_position


def test_runtime_snapshot_is_bounded_and_marks_bot_position():
    engine = SimpleNamespace(
        scanner_active_symbol="PROM/USDT:USDT",
        current_utbreakout_candidate_symbol=None,
        quad_alpha_last_status={
            "PROM/USDT": {
                "quad_alpha": {
                    "utbreak": {"label": "UTBreak", "light": "green", "side": "long", "reason": "accepted"},
                    "rspt": {"label": "RSPT-v3", "light": "yellow", "reason": "waiting"},
                }
            }
        },
        utbreakout_trailing_states={
            "PROM/USDT:USDT": {
                "strategy": "ADAPTIVE_BREAKOUT_TREND",
                "entry_price": 10,
                "last_stop_price": 9,
                "planned_tp_orders": [{"price": 12}, {"price": 14}],
            }
        },
    )
    controller = SimpleNamespace(
        engines={"signal": engine},
        status_data={},
        is_paused=False,
        get_active_strategy_params=lambda: {
            "active_strategy": "quad_alpha_v1",
            "UTBotFilteredBreakoutV1": {},
        },
        get_exchange_mode=lambda: "binance_testnet",
        _get_current_symbol=lambda: "PROM/USDT:USDT",
    )

    snapshot = build_desktop_monitor_snapshot(controller)

    assert snapshot["bot"]["current_symbol"] == "PROM/USDT"
    assert snapshot["strategies"][0]["state"] == "valid"
    assert snapshot["strategies"][1]["state"] == "waiting"
    assert snapshot["position_hints"][0]["strategy"] == "ADAPTIVE_BREAKOUT_TREND"


def test_exchange_position_and_orders_are_read_only_normalized():
    position = normalize_position(
        {
            "symbol": "PROM/USDT:USDT",
            "contracts": 100,
            "side": "long",
            "entryPrice": 10,
            "markPrice": 11,
            "notional": 1100,
            "leverage": 5,
            "unrealizedPnl": 100,
            "info": {"liquidationPrice": "4"},
        }
    )
    order = normalize_order(
        {
            "symbol": "PROM/USDT:USDT",
            "side": "sell",
            "type": "stop_market",
            "stopPrice": 9,
            "reduceOnly": True,
        }
    )
    runtime = {
        "position_hints": [
            {"symbol": "PROM/USDT", "strategy": "UTBreak", "source": "BOT"}
        ]
    }

    classified = classify_position(position, runtime)

    assert classified["source"] == "BOT"
    assert classified["strategy"] == "UTBreak"
    assert classified["roe_percent"] > 40
    assert order["price"] == 9
    assert order["reduce_only"] is True


def test_position_without_runtime_hint_is_not_mislabeled_as_bot():
    position = normalize_position(
        {
            "symbol": "BTC/USDT:USDT",
            "info": {"positionAmt": "-0.01", "entryPrice": "50000", "markPrice": "49000"},
        }
    )

    classified = classify_position(position, {})

    assert classified["side"] == "SHORT"
    assert classified["source"] == "MANUAL / UNKNOWN"


def test_missing_exchange_leverage_uses_notional_to_margin_ratio():
    position = normalize_position(
        {
            "symbol": "KORU/USDT:USDT",
            "contracts": 22.54,
            "entryPrice": 20.44,
            "markPrice": 20.31,
            "notional": 457.86,
            "leverage": None,
            "initialMargin": 76.31,
        }
    )

    assert position["leverage"] == 6.0


def test_live_status_symbol_wins_over_stale_internal_candidate():
    engine = SimpleNamespace(
        scanner_active_symbol=None,
        current_utbreakout_candidate_symbol=None,
        adaptive_breakout_trend_last_status={},
        utbreakout_trailing_states={},
    )
    controller = SimpleNamespace(
        engines={"signal": engine},
        status_data={
            "KORU/USDT:USDT": {
                "symbol": "KORU/USDT:USDT",
                "pos_side": "NONE",
                "price": 20.2,
            }
        },
        is_paused=False,
        get_active_strategy_params=lambda: {
            "active_strategy": "adaptive_breakout_trend_v1"
        },
        get_exchange_mode=lambda: "binance_mainnet",
        _get_current_symbol=lambda: "INTC/USDT:USDT",
    )

    snapshot = build_desktop_monitor_snapshot(controller)

    assert snapshot["bot"]["current_symbol"] == "KORU/USDT"


def test_monitor_exposes_latest_entry_block_in_korean():
    event = {
        "ts": 100.0,
        "symbol": "CYS/USDT:USDT",
        "stage": "AUTO_ENTRY_BRIDGE_BLOCKED",
        "status": "READY_TOO_OLD",
        "data": {
            "reason": "STATUS_READY event is too old",
            "ready_age_sec": 1800,
            "max_ready_age_sec": 1200,
        },
    }
    engine = SimpleNamespace(
        scanner_active_symbol=None,
        current_utbreakout_candidate_symbol="CYS/USDT:USDT",
        adaptive_breakout_trend_last_status={
            "CYS/USDT:USDT": {
                "strategy": "ADAPTIVE_BREAKOUT_TREND",
                "symbol": "CYS/USDT:USDT",
                "stage": "entry_ready",
                "accepted_code": "ACCEPTED_ENTRY",
                "accepted_side": "long",
                "reason": "ACCEPTED_ENTRY: trend",
            }
        },
        last_entry_reason={"CYS/USDT:USDT": "ACCEPTED_ENTRY: trend"},
        utbreakout_trailing_states={},
        _utbreakout_recent_trace_events=lambda symbol, limit=80: [event],
    )
    controller = SimpleNamespace(
        engines={"signal": engine},
        status_data={},
        is_paused=False,
        get_active_strategy_params=lambda: {
            "active_strategy": "adaptive_breakout_trend_v1"
        },
        get_exchange_mode=lambda: "binance_mainnet",
        _get_current_symbol=lambda: "CYS/USDT:USDT",
    )

    snapshot = build_desktop_monitor_snapshot(controller)

    diagnostic = snapshot["entry_diagnostic"]
    assert diagnostic["symbol"] == "CYS/USDT:USDT"
    assert "신호가 오래되어" in diagnostic["message"]
    assert "30.0분" in diagnostic["message"]


def test_entry_reason_translation_covers_core_trend_waits():
    assert "단기·중기·장기" in explain_entry_reason_ko(
        "Adaptive Breakout Trend waiting: multi_horizon_direction_not_aligned"
    )
    assert "모멘텀이 약해" in explain_entry_reason_ko("momentum_strength_too_low")

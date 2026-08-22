import json
from types import SimpleNamespace

from bot_runtime.desktop_monitor import build_desktop_monitor_snapshot
from bot_runtime.entry_reason_ko import explain_entry_reason_ko
from scripts.desktop_monitor_stream import (
    _apply_option_mark_candle,
    OptionsMonitorSource,
    classify_position,
    normalize_option_position,
    normalize_order,
    normalize_position,
)


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
        last_protection_order_status={
            "PROM/USDT:USDT": {
                "status": "OK",
                "fetch_ok": True,
                "sl_present": True,
                "stop_price": 9.5,
                "tp_orders": [{"price": 12.5}],
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
    assert snapshot["position_hints"][0]["stop_price"] == 9.5
    assert snapshot["position_hints"][0]["tp_prices"] == [12.5]


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


def test_bot_option_position_exposes_live_premium_and_software_protection():
    position = normalize_option_position(
        {
            "symbol": "SOL-260828-88-C",
            "side": "LONG",
            "quantity": "10",
            "entryPrice": "2.00",
            "markPrice": "2.50",
        },
        tracked={
            "symbol": "SOL-260828-88-C",
            "underlying": "SOLUSDT",
            "side": "CALL",
            "quantity": 10,
            "entry_price": 2.0,
            "entry_total_usdt": 20.2,
            "peak_mark": 4.2,
            "signal_strategy": "ADAPTIVE_TREND",
        },
        mark={"markPrice": "2.60", "markIV": "0.60", "delta": "0.55"},
        options_config={"stop_loss_pct": 0.55, "take_profit_pct": 3.0},
    )

    assert position["source"] == "BOT"
    assert position["option_type"] == "CALL"
    assert position["mark_price"] == 2.6
    assert round(position["unrealized_pnl"], 8) == 6.0
    assert round(position["return_percent"], 8) == 30.0
    assert round(position["hard_stop_price"], 8) == 0.9
    assert position["hard_target_price"] == 8.0
    assert round(position["trailing_floor"], 8) == 2.73
    assert position["delta"] == 0.55


def test_manual_option_position_does_not_claim_bot_protection():
    position = normalize_option_position(
        {
            "symbol": "ETH-260828-5000-P",
            "side": "LONG",
            "quantity": "1",
            "entryPrice": "10",
            "markPrice": "12",
        },
        options_config={"stop_loss_pct": 0.55, "take_profit_pct": 3.0},
    )

    assert position["source"] == "MANUAL / UNKNOWN"
    assert position["option_type"] == "PUT"
    assert position["hard_stop_price"] is None
    assert position["hard_target_price"] is None


def test_option_live_mark_updates_current_candle_instead_of_drifting_line():
    candles = [
        {
            "ts": 120_000,
            "open": 2.0,
            "high": 2.1,
            "low": 1.9,
            "close": 2.0,
            "volume": 1.0,
        }
    ]

    updated = _apply_option_mark_candle(candles, 2.5, now_ms=125_000)

    assert updated[-1]["ts"] == 120_000
    assert updated[-1]["close"] == 2.5
    assert updated[-1]["high"] == 2.5


def test_options_monitor_source_combines_exchange_state_and_public_chart(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (tmp_path / "config.json").write_text(
        json.dumps({"options_trading": {"enabled": True}}),
        encoding="utf-8",
    )
    (runtime / "options_trading_state.json").write_text(
        json.dumps(
            {
                "active_position": {
                    "symbol": "SOL-TEST-C",
                    "underlying": "SOLUSDT",
                    "side": "CALL",
                    "quantity": 2,
                    "entry_price": 2.0,
                    "entry_total_usdt": 4.1,
                    "peak_mark": 2.5,
                    "signal_strategy": "ADAPTIVE_TREND",
                },
                "cash_bankroll_usdt": 95.9,
                "capital_limit_usdt": 100.0,
                "last_reason": "보유 관리 중",
                "last_manage_success_ts": 100.0,
                "manage_error_streak": 0,
            }
        ),
        encoding="utf-8",
    )
    source = OptionsMonitorSource(
        tmp_path,
        {"options_trading": {"enabled": True}},
        "1m",
        60,
    )
    source.client = SimpleNamespace(
        positions=lambda: [
            {
                "symbol": "SOL-TEST-C",
                "side": "LONG",
                "quantity": "2",
                "entryPrice": "2.0",
                "markPrice": "2.4",
            }
        ],
        mark_price=lambda symbol: [
            {"symbol": symbol, "markPrice": "2.4", "delta": "0.5"}
        ],
        klines=lambda symbol, interval, limit: [
            [60_000, "2.0", "2.1", "1.9", "2.0", "10"],
            [120_000, "2.0", "2.2", "2.0", "2.1", "8"],
        ],
    )

    snapshot = source.snapshot()

    assert snapshot["enabled"] is True
    assert snapshot["selected_symbol"] == "SOL-TEST-C"
    assert snapshot["positions"][0]["exchange_verified"] is True
    assert snapshot["positions"][0]["mark_price"] == 2.4
    assert snapshot["positions"][0]["source"] == "BOT"
    assert snapshot["candles"][-1]["close"] == 2.4
    assert snapshot["error"] is None


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


def test_monitor_prefers_strategy_root_cause_over_no_status_ready_consequence():
    events = [
        {
            "ts": 100.0,
            "symbol": "BNB/USDT:USDT",
            "stage": "SIGNAL_CALCULATED",
            "status": "RESULT",
            "data": {
                "reason": (
                    "Adaptive Breakout Trend waiting: "
                    "multi_horizon_direction_not_aligned"
                )
            },
        },
        {
            "ts": 101.0,
            "symbol": "BNB/USDT:USDT",
            "stage": "AUTO_ENTRY_BRIDGE_BLOCKED",
            "status": "NO_STATUS_READY",
            "data": {
                "reason": (
                    "no live STATUS_READY and no accepted diagnostic/plan"
                )
            },
        },
    ]
    engine = SimpleNamespace(
        scanner_active_symbol=None,
        current_utbreakout_candidate_symbol="BNB/USDT:USDT",
        adaptive_breakout_trend_last_status={
            "BNB/USDT:USDT": {
                "strategy": "ADAPTIVE_BREAKOUT_TREND",
                "symbol": "BNB/USDT:USDT",
                "stage": "waiting",
                "reason": (
                    "Adaptive Breakout Trend waiting: "
                    "multi_horizon_direction_not_aligned"
                ),
            }
        },
        last_entry_reason={},
        utbreakout_trailing_states={},
        _utbreakout_recent_trace_events=lambda symbol, limit=80: events,
    )
    controller = SimpleNamespace(
        engines={"signal": engine},
        status_data={},
        is_paused=False,
        get_active_strategy_params=lambda: {
            "active_strategy": "adaptive_breakout_trend_v1"
        },
        get_exchange_mode=lambda: "binance_mainnet",
        _get_current_symbol=lambda: "BNB/USDT:USDT",
    )

    diagnostic = build_desktop_monitor_snapshot(controller)["entry_diagnostic"]

    assert "단기·중기·장기 추세 방향" in diagnostic["message"]
    assert diagnostic["stage"] == "SIGNAL_CALCULATED"
    assert diagnostic["code"] == "RESULT"


def test_entry_reason_translation_covers_core_trend_waits():
    assert "단기·중기·장기" in explain_entry_reason_ko(
        "Adaptive Breakout Trend waiting: multi_horizon_direction_not_aligned"
    )
    assert "모멘텀이 약해" in explain_entry_reason_ko("momentum_strength_too_low")

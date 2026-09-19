import asyncio
import inspect
from types import SimpleNamespace

import pandas as pd
import pytest
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

import emas
from bot_runtime.controller_ema200_utbot_rsi import ControllerEMA200UTBotRSIMixin
from bot_runtime.database import DBManager
from bot_runtime.ema200_utbot_rsi import (
    EMA200_BINANCE_TOP10_BASES,
    EMA200_BINANCE_TOP10_SYMBOLS,
    EMA200_UTBOT_RSI_STRATEGY,
    build_ema200_utbot_rsi_risk_plan,
    calculate_ema200_utbot_rsi_emergency_stop_price,
    count_ema200_consecutive_losses,
    ema200_small_account_margin_percent,
    evaluate_ema200_utbot_rsi_entry,
    evaluate_ema200_utbot_rsi_loss_gate,
    is_ema200_utbot_rsi_symbol_allowed,
    normalize_ema200_utbot_rsi_config,
)
from bot_runtime.strategy_registry import CORE_STRATEGIES


def test_strategy_registered_in_core_strategies():
    assert EMA200_UTBOT_RSI_STRATEGY in CORE_STRATEGIES


def test_long_requires_ordered_ut_then_rsi_above_50_and_rising():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal == "long"
    assert detail["ut_precedes_rsi"] is True
    assert detail["rsi_above_and_rising"] is True


def test_long_rejects_current_rsi_condition_when_ut_buy_did_not_happen_first():
    signal, reason, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=3_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None
    assert detail["ut_precedes_rsi"] is False
    assert "먼저 확정되지 않음" in reason


def test_same_completed_candle_ut_and_rsi_condition_is_rejected_as_unknown_order():
    signal, reason, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=2_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None
    assert detail["ut_precedes_rsi"] is False
    assert "먼저 확정되지 않음" in reason


def test_long_accepts_rsi_already_above_50_when_it_is_still_rising():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=55.0,
        curr_rsi=57.0,
        rsi_signal_ts=2_000,
    )
    assert signal == "long"
    assert detail["rsi_cross_up"] is False
    assert detail["rsi_above_and_rising"] is True


@pytest.mark.parametrize(
    ("ut_state", "previous", "current"),
    [
        ("long", 55.0, 54.0),
        ("long", 55.0, 55.0),
        ("long", 49.0, 50.0),
        ("short", 45.0, 46.0),
        ("short", 45.0, 45.0),
        ("short", 51.0, 50.0),
    ],
)
def test_rsi_must_be_strictly_on_correct_side_and_move_in_entry_direction(
    ut_state,
    previous,
    current,
):
    signal, _, _ = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0 if ut_state == "long" else 90.0,
        ema200=100.0,
        ut_state=ut_state,
        ut_last_signal_side=ut_state,
        ut_last_signal_ts=1_000,
        prev_rsi=previous,
        curr_rsi=current,
        rsi_signal_ts=2_000,
    )
    assert signal is None


def test_short_is_exact_inverse():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=90.0,
        ema200=100.0,
        ut_state="short",
        ut_last_signal_side="short",
        ut_last_signal_ts=1_000,
        prev_rsi=48.0,
        curr_rsi=47.0,
        rsi_signal_ts=2_000,
    )
    assert signal == "short"
    assert detail["ema_short_ok"] is True
    assert detail["rsi_cross_down"] is False
    assert detail["rsi_below_and_falling"] is True


def test_fixed_universe_is_exactly_ten_non_stable_binance_perpetual_assets():
    assert EMA200_BINANCE_TOP10_BASES == (
        "BTC",
        "ETH",
        "BNB",
        "XRP",
        "SOL",
        "TRX",
        "ZEC",
        "HYPE",
        "DOGE",
        "XMR",
    )
    assert len(EMA200_BINANCE_TOP10_SYMBOLS) == 10
    assert not {"USDT", "USDC", "DAI"} & set(EMA200_BINANCE_TOP10_BASES)
    assert all(symbol.endswith("/USDT:USDT") for symbol in EMA200_BINANCE_TOP10_SYMBOLS)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("BTC/USDT:USDT", True),
        ("btcusdt", True),
        ("HYPE/USDT", True),
        ("ADA/USDT:USDT", False),
        ("BTC/USDC:USDC", False),
        ("BTC", False),
        ("USDT/USDT:USDT", False),
        (None, False),
    ],
)
def test_fixed_universe_guard_normalizes_common_symbol_forms(symbol, expected):
    assert is_ema200_utbot_rsi_symbol_allowed(symbol) is expected


def test_wrong_ema_side_blocks_signal():
    signal, _, _ = evaluate_ema200_utbot_rsi_entry(
        close_price=99.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None


def test_above_1000_risk_plan_sizes_from_loss_budget_and_emergency_distance():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=2000.0,
        free_balance=2000.0,
        entry_price=100.0,
        config={
            "risk_per_trade_percent": 0.5,
            "emergency_exit_percent": 5.0,
            "leverage": 5,
        },
    )
    assert plan["small_account_mode"] is False
    assert plan["risk_budget_usdt"] == 10.0
    assert plan["planned_notional"] == 200.0
    assert plan["planned_margin"] == 40.0
    assert plan["planned_emergency_loss_usdt"] == 10.0
    assert plan["planned_qty"] == 2.0


def test_small_account_first_stage_uses_half_equity_at_5x_without_stop():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=200.0,
        free_balance=200.0,
        entry_price=100.0,
        config={"emergency_exit_percent": 5.0, "leverage": 9},
        consecutive_losses=0,
    )

    assert plan["small_account_mode"] is True
    assert plan["margin_percent"] == 50.0
    assert plan["planned_margin"] == 100.0
    assert plan["leverage"] == 5
    assert plan["planned_notional"] == 500.0
    assert plan["planned_qty"] == 5.0
    assert plan["strategy_exit_only"] is True
    assert plan["emergency_stop_required"] is False
    assert plan["planned_emergency_loss_usdt"] is None


@pytest.mark.parametrize(
    ("loss_streak", "expected_margin_percent"),
    [(1, 35.0), (2, 25.0), (3, 15.0), (4, 10.0), (12, 10.0)],
)
def test_small_account_loss_ladder_reduces_next_position_and_enables_stop(
    loss_streak,
    expected_margin_percent,
):
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=200.0,
        free_balance=200.0,
        entry_price=100.0,
        config={"emergency_exit_percent": 5.0},
        consecutive_losses=loss_streak,
    )

    expected_margin = 200.0 * expected_margin_percent / 100.0
    assert plan["margin_percent"] == expected_margin_percent
    assert plan["planned_margin"] == expected_margin
    assert plan["planned_notional"] == expected_margin * 5
    assert plan["emergency_stop_required"] is True
    assert plan["strategy_exit_only"] is False
    assert plan["planned_emergency_loss_usdt"] == pytest.approx(
        expected_margin * 5 * 0.05
    )


def test_small_account_boundary_includes_exactly_1000_usdt():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=1000.0,
        free_balance=1000.0,
        entry_price=100.0,
        consecutive_losses=0,
    )
    assert plan["small_account_mode"] is True
    assert plan["planned_margin"] == 500.0
    assert plan["planned_notional"] == 2500.0


def test_consecutive_loss_helpers_reset_on_profit_or_break_even():
    assert count_ema200_consecutive_losses([-1.0, -2.0, 3.0, -4.0]) == 2
    assert count_ema200_consecutive_losses([0.0, -2.0]) == 0
    assert ema200_small_account_margin_percent(0) == 50.0
    assert ema200_small_account_margin_percent(99) == 10.0


def test_database_loss_streak_is_strategy_specific_and_restart_durable(tmp_path):
    db_path = tmp_path / "trades.sqlite3"
    db = DBManager(db_path)

    def close_trade(symbol, pnl, strategy):
        db.log_trade_entry(symbol, "long", 100.0, 1.0, strategy=strategy)
        assert db.log_trade_close(symbol, pnl, pnl, 100.0 + pnl, "test")

    close_trade("BTC/USDT", 4.0, EMA200_UTBOT_RSI_STRATEGY)
    close_trade("ETH/USDT", -8.0, "utbot")
    close_trade("SOL/USDT", -3.0, EMA200_UTBOT_RSI_STRATEGY)
    close_trade("XRP/USDT", -2.0, EMA200_UTBOT_RSI_STRATEGY)
    assert db.get_consecutive_strategy_losses(EMA200_UTBOT_RSI_STRATEGY) == 2
    db.conn.close()

    reopened = DBManager(db_path)
    assert reopened.get_consecutive_strategy_losses(EMA200_UTBOT_RSI_STRATEGY) == 2
    close_trade_db = reopened
    close_trade_db.log_trade_entry(
        "ADA/USDT", "long", 100.0, 1.0, strategy=EMA200_UTBOT_RSI_STRATEGY
    )
    assert close_trade_db.log_trade_close(
        "ADA/USDT", 1.0, 1.0, 101.0, "profit reset"
    )
    assert reopened.get_consecutive_strategy_losses(EMA200_UTBOT_RSI_STRATEGY) == 0
    reopened.conn.close()


def test_risk_plan_caps_position_to_available_margin_without_increasing_risk():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=2000.0,
        free_balance=5.0,
        entry_price=100.0,
        config={
            "risk_per_trade_percent": 0.5,
            "emergency_exit_percent": 5.0,
            "leverage": 5,
        },
    )
    assert plan["margin_cap_applied"] is True
    assert plan["planned_notional"] < 200.0
    assert plan["planned_emergency_loss_usdt"] < plan["risk_budget_usdt"]


def test_daily_and_weekly_limits_block_new_entries_only_via_gate_result():
    gate = evaluate_ema200_utbot_rsi_loss_gate(
        account_equity=1000.0,
        daily_realized_pnl=-21.0,
        weekly_realized_pnl=-21.0,
        config={
            "daily_loss_limit_percent": 2.0,
            "weekly_loss_limit_percent": 5.0,
        },
    )
    assert gate["allowed"] is False
    assert gate["daily_blocked"] is True
    assert gate["weekly_blocked"] is False


def test_weekly_limit_is_never_normalized_below_daily_limit():
    cfg = normalize_ema200_utbot_rsi_config(
        {
            "daily_loss_limit_percent": 6.0,
            "weekly_loss_limit_percent": 4.0,
        }
    )
    assert cfg["weekly_loss_limit_percent"] >= cfg["daily_loss_limit_percent"]


def test_config_normalization_keeps_fixed_safety_ranges_and_rejects_nonfinite_values():
    cfg = normalize_ema200_utbot_rsi_config(
        {
            "min_risk_per_trade_percent": 0.001,
            "max_risk_per_trade_percent": 99.0,
            "enabled": "false",
            "risk_per_trade_percent": float("nan"),
            "rsi_length": float("inf"),
            "min_leverage": 0,
            "max_leverage": 99,
            "leverage": float("inf"),
            "max_emergency_exit_percent": 99.0,
            "emergency_exit_percent": float("inf"),
            "max_daily_loss_limit_percent": 99.0,
            "daily_loss_limit_percent": 99.0,
            "max_weekly_loss_limit_percent": 99.0,
            "weekly_loss_limit_percent": 99.0,
        }
    )
    assert cfg["min_risk_per_trade_percent"] == 0.10
    assert cfg["enabled"] is False
    assert cfg["max_risk_per_trade_percent"] == 5.00
    assert cfg["risk_per_trade_percent"] == 0.50
    assert cfg["min_leverage"] == 1
    assert cfg["max_leverage"] == 10
    assert cfg["rsi_length"] == 14
    assert cfg["leverage"] == 5
    assert cfg["emergency_exit_percent"] == 5.0
    assert cfg["max_emergency_exit_percent"] == 30.0
    assert cfg["daily_loss_limit_percent"] == 20.0
    assert cfg["weekly_loss_limit_percent"] == 40.0


def test_emergency_stop_price_is_long_short_symmetric():
    config = {"emergency_exit_percent": 5.0}
    assert calculate_ema200_utbot_rsi_emergency_stop_price(
        side="long", entry_price=100.0, config=config
    ) == pytest.approx(95.0)
    assert calculate_ema200_utbot_rsi_emergency_stop_price(
        side="short", entry_price=100.0, config=config
    ) == pytest.approx(105.0)


def test_signal_uses_last_completed_candle_and_ignores_current_candle():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    rows = []
    for index in range(205):
        close = 100.0 + index
        rows.append([index, close - 1.0, close + 1.0, close - 2.0, close, 10.0])
    rows[-1][4] = 10_000.0
    frame = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    engine._calculate_utbot_signal = lambda df, params: (
        None,
        "no fresh signal",
        {"bias_side": "long", "signal_side": "long", "signal_ts": 100},
    )

    _, _, detail = engine._calculate_ema200_utbot_rsi_signal(
        frame,
        {"EMA200UTBotRSI2H": {"enabled": True}},
    )

    assert detail["closed_candle_ts"] == rows[-2][0]
    assert detail["closed_candle_close"] == rows[-2][4]
    assert detail["closed_candle_close"] != rows[-1][4]


def test_rsi_uses_wilder_sma_seed_then_recursive_smoothing():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    rsi = engine._calculate_wilder_rsi_for_ema200_strategy(
        [10.0, 11.0, 10.0, 12.0, 11.0],
        3,
    )

    assert rsi.iloc[:3].isna().all()
    assert rsi.iloc[3] == pytest.approx(75.0)
    assert rsi.iloc[4] == pytest.approx(54.5454545455)


def test_primary_and_exit_polling_are_fixed_to_2h():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    params = {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"timeframe": "4h"},
    }
    engine.get_runtime_trade_config = lambda: {
        "common_settings": {"entry_timeframe": "15m", "exit_timeframe": "4h"},
        "strategy_params": params,
    }
    engine.get_runtime_common_settings = lambda: {
        "entry_timeframe": "15m",
        "exit_timeframe": "4h",
    }
    engine.get_runtime_strategy_params = lambda: params

    assert engine._get_primary_poll_timeframe() == "2h"
    assert engine._get_exit_timeframe("BTC/USDT") == "2h"

    fixed_scanner_source = inspect.getsource(
        emas.SignalEngine._scan_and_trade_ema200_binance_top10
    )
    high_volume_source = inspect.getsource(emas.SignalEngine.scan_and_trade_high_volume)
    assert "'2h'" in fixed_scanner_source
    assert "EMA200_BINANCE_TOP10_SYMBOLS" in fixed_scanner_source
    assert "_scan_and_trade_ema200_binance_top10" in high_volume_source


def test_fixed_top10_scanner_checks_every_symbol_on_2h_without_volume_selection():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    calls = []

    def fetch_ohlcv(symbol, timeframe, limit):
        calls.append((symbol, timeframe, limit))
        return [
            [1, 1.0, 2.0, 0.5, 1.5, 10.0],
            [2, 1.5, 2.0, 0.5, 1.4, 10.0],
            [3, 1.4, 2.0, 0.5, 1.3, 10.0],
        ]

    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    engine.ctrl = SimpleNamespace(is_paused=False)
    engine.scanner_active_symbol = None
    engine.ema200_top10_scan_cursor = 0
    engine.last_entry_reason = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine._collect_primary_strategy_context = lambda *args, **kwargs: {
        "precomputed": {},
    }

    async def no_signal(*args, **kwargs):
        return None, None, None, None, None, None

    engine._calculate_strategy_signal = no_signal

    asyncio.run(engine._scan_and_trade_ema200_binance_top10())

    assert calls == [
        (symbol, "2h", 300) for symbol in EMA200_BINANCE_TOP10_SYMBOLS
    ]

    calls.clear()
    asyncio.run(engine._scan_and_trade_ema200_binance_top10())
    rotated = EMA200_BINANCE_TOP10_SYMBOLS[1:] + EMA200_BINANCE_TOP10_SYMBOLS[:1]
    assert calls == [(symbol, "2h", 300) for symbol in rotated]


def test_high_volume_scanner_bypasses_coin_selector_for_ema200_strategy():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    calls = []
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }

    async def fixed_scan():
        calls.append("fixed")

    engine._scan_and_trade_ema200_binance_top10 = fixed_scan
    engine._get_coin_selector_config = lambda: (_ for _ in ()).throw(
        AssertionError("CoinSelector must not run for the fixed EMA200 universe")
    )

    asyncio.run(engine.scan_and_trade_high_volume())

    assert calls == ["fixed"]


def test_ema200_entry_guard_blocks_every_symbol_outside_fixed_top10():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    notices = []

    async def notify(message):
        notices.append(message)

    engine.ctrl = SimpleNamespace(notify=notify)
    engine.last_entry_reason = {}
    engine.is_user_custom_entry_mode_enabled = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }

    asyncio.run(engine.entry("ADA/USDT:USDT", "long", 1.0))

    assert "EMA200_FIXED_TOP10_ONLY" in engine.last_entry_reason["ADA/USDT:USDT"]
    assert notices and "진입 차단" in notices[0]


def test_latest_strategy_evaluation_is_saved_for_telegram_status():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.last_ema200_utbot_rsi_status = {}
    expected_detail = {
        "closed_candle_ts": 123,
        "closed_candle_close": 101.0,
        "ema200": 100.0,
        "ut_state": "long",
        "prev_rsi": 49.0,
        "curr_rsi": 51.0,
    }
    engine._calculate_ema200_utbot_rsi_signal = lambda df, params: (
        "long",
        "ready",
        dict(expected_detail),
    )

    context = engine._collect_primary_strategy_context(
        "BTC/USDT",
        pd.DataFrame(),
        {"active_strategy": EMA200_UTBOT_RSI_STRATEGY},
        EMA200_UTBOT_RSI_STRATEGY,
    )

    assert context["precomputed"][EMA200_UTBOT_RSI_STRATEGY][0] == "long"
    saved = engine.last_ema200_utbot_rsi_status["BTC/USDT"]
    assert all(saved[key] == value for key, value in expected_detail.items())
    assert int(saved["evaluated_at_ns"]) > 0


@pytest.mark.parametrize(
    ("current_side", "ut_signal", "exit_label", "enabled"),
    [
        ("long", "short", "EMA200_UTBOT_RSI_UT_SELL", False),
        ("short", "long", "EMA200_UTBOT_RSI_UT_BUY", True),
    ],
)
def test_fresh_opposite_ut_signal_mechanically_exits_before_optional_filters(
    current_side,
    ut_signal,
    exit_label,
    enabled,
):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.market_data_exchange = SimpleNamespace(
        fetch_ohlcv=lambda *args, **kwargs: [
            [1, 1, 2, 0.5, 1.5, 10],
            [2, 1.5, 2, 0.5, 1.4, 10],
            [3, 1.4, 2, 0.5, 1.3, 10],
        ]
    )
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"enabled": enabled},
    }
    engine.get_runtime_common_settings = lambda: (_ for _ in ()).throw(
        AssertionError("optional exit filters must not run")
    )
    engine._calculate_utbot_signal = lambda df, params: (
        ut_signal,
        "fresh opposite",
        {"bias_side": ut_signal},
    )
    engine._update_stateful_diag = lambda *args, **kwargs: None
    engine.last_entry_reason = {}
    exit_calls = []

    async def exit_position(symbol, reason):
        exit_calls.append((symbol, reason))

    async def fetch_position(symbol):
        return True, None

    engine.exit_position = exit_position
    engine._fetch_server_position_checked = fetch_position

    processed = asyncio.run(
        engine.process_exit_candle("BTC/USDT", "2h", current_side)
    )

    assert processed is True
    assert exit_calls == [("BTC/USDT", exit_label)]


def test_mechanical_exit_retries_same_candle_when_position_remains_open():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.market_data_exchange = SimpleNamespace(
        fetch_ohlcv=lambda *args, **kwargs: [
            [1, 1, 2, 0.5, 1.5, 10],
            [2, 1.5, 2, 0.5, 1.4, 10],
            [3, 1.4, 2, 0.5, 1.3, 10],
        ]
    )
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine._calculate_utbot_signal = lambda df, params: (
        "short",
        "fresh sell",
        {"bias_side": "short"},
    )
    engine._update_stateful_diag = lambda *args, **kwargs: None
    engine.last_entry_reason = {}

    async def exit_position(symbol, reason):
        return None

    async def fetch_position(symbol):
        return True, {"side": "long", "contracts": 1.0}

    engine.exit_position = exit_position
    engine._fetch_server_position_checked = fetch_position

    assert asyncio.run(engine.process_exit_candle("BTC/USDT", "2h", "long")) is False
    assert "재시도" in engine.last_entry_reason["BTC/USDT"]


def test_later_loss_stage_requires_stop_and_never_fixed_tp_for_strategy():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine.get_runtime_common_settings = lambda: {
        "tp_sl_enabled": False,
        "take_profit_enabled": True,
        "stop_loss_enabled": False,
    }

    expected = engine._protection_expected_from_config(
        "BTC/USDT",
        {"side": "long", "contracts": 1.0},
    )

    assert expected == (False, True)


def test_first_small_account_stage_is_durably_recognized_as_strategy_managed():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    record = SimpleNamespace(
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        metadata={"strategy_managed_no_stop": True},
    )
    engine.trading_state_store = SimpleNamespace(
        active_for_symbol=lambda symbol: [record]
    )

    assert engine._protection_expected_from_config(
        "BTC/USDT",
        {"side": "long", "contracts": 1.0},
    ) == (False, False)


def test_entry_branch_conditionally_places_emergency_stop_after_first_loss():
    source = inspect.getsource(emas.SignalEngine.entry)
    protection_branch = source.rsplit(
        "elif active_strategy == EMA200_UTBOT_RSI_STRATEGY:", 1
    )[1].split("elif active_strategy in UTBREAKOUT_STRATEGIES:", 1)[0]
    assert "emergency_stop_required" in protection_branch
    assert "tp_distance=None" in protection_branch
    assert "sl_distance=emergency_distance" in protection_branch
    assert "notify_after_place=False" in protection_branch
    assert "거래소 Stop 없음" in protection_branch

    finalization_source = source[source.index("ema200_strategy_only_no_stop = bool("):]
    assert "strategy_managed_no_stop=True" in finalization_source
    assert "STRATEGY_MANAGED_NO_STOP" in finalization_source


def test_minimum_notional_branch_blocks_instead_of_auto_increasing_strategy_size():
    source = inspect.getsource(emas.SignalEngine.entry)
    start = source.index("if min_notional > 0 and target_notional < min_notional:")
    auto_bump = source.index("# If balance/leverage can support exchange minimum", start)
    strategy_block = source[start:auto_bump]
    assert "active_strategy == EMA200_UTBOT_RSI_STRATEGY" in strategy_block
    assert "return" in strategy_block
    assert "target_notional = min_notional" not in strategy_block


def test_common_daily_breaker_is_bypassed_without_touching_mandatory_safety_paths():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine._fetch_active_position_symbols_checked = lambda: (_ for _ in ()).throw(
        AssertionError("common forced-close path must not run")
    )

    assert asyncio.run(engine.check_daily_loss_limit()) is False
    entry_source = inspect.getsource(emas.SignalEngine.entry)
    assert "_submit_idempotent_crypto_entry" in entry_source
    assert "_preflight_liquidation_safety" in entry_source
    assert "_verify_actual_liquidation_safety" in entry_source


class _TelegramConfig(dict):
    def __init__(self):
        super().__init__({"binance_futures": {"strategy_params": {}}})
        self.updates = []

    async def update_value(self, path, value):
        self.updates.append((list(path), value))


class _TelegramApp:
    def __init__(self):
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


class _TelegramMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _TelegramQuery:
    def __init__(self, data):
        self.data = data
        self.edits = []

    async def answer(self):
        return None

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


def _registered_telegram_controller():
    controller = ControllerEMA200UTBotRSIMixin()
    controller.tg_app = _TelegramApp()
    controller.cfg = _TelegramConfig()
    controller.get_active_trade_section = lambda: "binance_futures"
    controller.is_upbit_mode = lambda: False
    controller._register_ema200_utbot_rsi_handlers(
        lambda callback: callback,
        filters.TEXT & ~filters.COMMAND,
    )
    return controller


def test_telegram_status_uses_real_evaluation_order_when_2h_timestamps_tie():
    controller = _registered_telegram_controller()
    controller.db = SimpleNamespace(
        get_daily_stats=lambda: (0, 0.0),
        get_weekly_stats=lambda: (0, 0.0),
        get_consecutive_strategy_losses=lambda strategy: 1,
    )
    base_detail = {
        "closed_candle_ts": 1_000,
        "closed_candle_close": 100.0,
        "ema200": 99.0,
        "ut_state": "long",
        "ut_last_signal_side": "long",
        "prev_rsi": 49.0,
        "curr_rsi": 49.5,
    }
    controller.engines = {
        "signal": SimpleNamespace(
            last_ema200_utbot_rsi_status={
                "BTC/USDT": {**base_detail, "evaluated_at_ns": 10},
                "ETH/USDT": {**base_detail, "evaluated_at_ns": 20},
            },
            last_entry_reason={"ETH/USDT": "latest evaluation"},
        )
    }

    status = asyncio.run(controller._ema200_utbot_rsi_status_text())

    assert "최근 조건 (ETH/USDT)" in status
    assert "최근 평가 종목(2개 기록): ETH/USDT, BTC/USDT" in status
    assert "스캔 종목: BTC, ETH, BNB, XRP, SOL, TRX, ZEC, HYPE, DOGE, XMR" in status
    assert "소액계좌 다음 단계(해당 시): 증거금 35% / 5x" in status
    assert "UT 반대 신호 + 비상 Stop" in status


def test_telegram_status_shows_actual_large_account_entry_amounts_and_ratios():
    controller = _registered_telegram_controller()
    controller.db = SimpleNamespace(
        get_daily_stats=lambda: (0, 0.0),
        get_weekly_stats=lambda: (0, 0.0),
        get_consecutive_strategy_losses=lambda strategy: 0,
    )

    async def get_balance_info():
        return 5000.0, 5000.0, 0.0

    controller.engines = {
        "signal": SimpleNamespace(
            get_balance_info=get_balance_info,
            last_ema200_utbot_rsi_status={},
            last_entry_reason={},
        )
    }

    status = asyncio.run(controller._ema200_utbot_rsi_status_text())

    assert "현재 계좌: 5000.00 USDT → 위험예산 방식" in status
    assert "1회 허용손실: 25.00 USDT (계좌의 0.50%)" in status
    assert "비상 손절 가격거리: 진입가 대비 5.00%" in status
    assert "예상 명목 포지션: 500.00 USDT (계좌의 10.00%)" in status
    assert "예상 사용 증거금: 100.00 USDT (계좌의 2.00%, 5x)" in status


def test_telegram_sizing_preview_recalculates_entry_ratio_for_wider_stop():
    controller = _registered_telegram_controller()

    async def get_balance_info():
        return 5000.0, 5000.0, 0.0

    controller.engines = {
        "signal": SimpleNamespace(get_balance_info=get_balance_info)
    }
    cfg = normalize_ema200_utbot_rsi_config(
        {
            "risk_per_trade_percent": 0.5,
            "emergency_exit_percent": 25.0,
            "leverage": 5,
        }
    )

    preview = asyncio.run(
        controller._ema200_utbot_rsi_sizing_preview(cfg, loss_streak=0)
    )

    assert "비상 손절 가격거리: 진입가 대비 25.00%" in preview
    assert "예상 명목 포지션: 100.00 USDT (계좌의 2.00%)" in preview
    assert "예상 사용 증거금: 20.00 USDT (계좌의 0.40%, 5x)" in preview


def test_telegram_emergency_help_explains_distance_and_inverse_position_sizing():
    help_text = ControllerEMA200UTBotRSIMixin._ema200_utbot_rsi_help_text(
        "emergency"
    )

    assert "포지션에 넣는 비율이 아니라" in help_text
    assert "손절거리 5%" in help_text
    assert "명목 포지션 약 500 USDT" in help_text
    assert "증거금 약 100 USDT(계좌의 2%)" in help_text
    assert "손절거리 25%" in help_text
    assert "명목 포지션 약 100 USDT" in help_text
    assert "진입금액은 작아집니다" in help_text
    assert "청산가보다 늦어질 수 있어 진입 자체가 차단" in help_text


def test_telegram_keyboard_labels_emergency_percent_as_stop_distance():
    controller = _registered_telegram_controller()
    labels = [
        button.text
        for row in controller._build_ema200_utbot_rsi_keyboard().inline_keyboard
        for button in row
    ]

    assert "손절거리 5%" in labels
    assert "✍️ 손절거리 직접입력" in labels
    assert all("비상탈출" not in label for label in labels)


@pytest.mark.parametrize("raw_value", ["9", "nan", "inf"])
def test_telegram_direct_risk_input_rejects_out_of_range_and_nonfinite(raw_value):
    controller = _registered_telegram_controller()
    handler, group = next(
        (handler, group)
        for handler, group in controller.tg_app.handlers
        if isinstance(handler, MessageHandler)
    )
    message = _TelegramMessage(raw_value)
    context = SimpleNamespace(user_data={"ema200_utbot_rsi_custom": "risk"})

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(handler.callback(SimpleNamespace(message=message), context))

    assert group == -2
    assert controller.cfg.updates == []
    assert message.replies


def test_telegram_custom_handler_without_state_returns_without_stopping_other_groups():
    controller = _registered_telegram_controller()
    handler, group = next(
        (handler, group)
        for handler, group in controller.tg_app.handlers
        if isinstance(handler, MessageHandler)
    )
    message = _TelegramMessage("ordinary text")

    result = asyncio.run(
        handler.callback(
            SimpleNamespace(message=message),
            SimpleNamespace(user_data={}),
        )
    )

    assert group == -2
    assert result is None
    assert message.replies == []


def test_telegram_activation_is_blocked_when_position_is_open():
    controller = _registered_telegram_controller()

    async def has_open_position():
        return True, "BTC/USDT"

    controller._ema200_utbot_rsi_has_open_position = has_open_position
    handler = next(
        handler
        for handler, _ in controller.tg_app.handlers
        if isinstance(handler, CallbackQueryHandler)
    )
    query = _TelegramQuery("e2h:activate")

    asyncio.run(
        handler.callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}),
        )
    )

    assert controller.cfg.updates == []
    assert query.edits
    assert "열린 포지션" in query.edits[-1]


def test_telegram_activation_fails_closed_when_position_lookup_fails():
    controller = _registered_telegram_controller()

    async def position_lookup_failed():
        return None, "포지션 조회 실패: timeout"

    controller._ema200_utbot_rsi_has_open_position = position_lookup_failed
    handler = next(
        handler
        for handler, _ in controller.tg_app.handlers
        if isinstance(handler, CallbackQueryHandler)
    )
    query = _TelegramQuery("e2h:activate")

    asyncio.run(
        handler.callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}),
        )
    )

    assert controller.cfg.updates == []
    assert query.edits
    assert "전략 변경을 중단" in query.edits[-1]


def test_telegram_quick_leverage_button_updates_strategy_leverage():
    controller = _registered_telegram_controller()
    handler = next(
        handler
        for handler, _ in controller.tg_app.handlers
        if isinstance(handler, CallbackQueryHandler)
    )
    query = _TelegramQuery("e2h:leverage:5")

    asyncio.run(
        handler.callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}),
        )
    )

    assert controller.cfg.updates == [
        (
            [
                "binance_futures",
                "strategy_params",
                "EMA200UTBotRSI2H",
                "leverage",
            ],
            5,
        )
    ]
    assert query.edits


def test_telegram_daily_input_raises_weekly_limit_to_preserve_valid_ordering():
    controller = _registered_telegram_controller()
    handler, _ = next(
        (handler, group)
        for handler, group in controller.tg_app.handlers
        if isinstance(handler, MessageHandler)
    )
    message = _TelegramMessage("6")
    context = SimpleNamespace(user_data={"ema200_utbot_rsi_custom": "daily"})

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(handler.callback(SimpleNamespace(message=message), context))

    assert controller.cfg.updates == [
        (
            [
                "binance_futures",
                "strategy_params",
                "EMA200UTBotRSI2H",
                "daily_loss_limit_percent",
            ],
            6.0,
        ),
        (
            [
                "binance_futures",
                "strategy_params",
                "EMA200UTBotRSI2H",
                "weekly_loss_limit_percent",
            ],
            6.0,
        ),
    ]

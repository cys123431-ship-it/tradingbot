from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot_runtime.signal_alpha as signal_alpha_module
from bot_runtime.diagnostics import _safe_float_or_none
from utbreakout.adaptive_breakout_trend import (
    ADAPTIVE_BREAKOUT_TREND_STRATEGY,
    _normalized_momentum_horizons,
    default_adaptive_breakout_trend_config,
    evaluate_adaptive_breakout_trend,
    normalize_adaptive_breakout_trend_config,
)
from bot_runtime.signal_alpha import SignalAlphaMixin
from bot_runtime.signal_entry import build_durable_entry_plan_summary
from bot_runtime.signal_scanner import SignalScannerMixin
from utbreakout.dynamic_leverage import select_dynamic_leverage
from utbreakout.relative_strength_pullback import completed_candle_rows
from utbreakout.risk import calculate_risk_plan
from utbreakout.risk_budget import (
    cap_utbreakout_risk_plan_to_margin,
    resolve_utbreakout_risk_budget,
)


ROOT = Path(__file__).resolve().parents[1]
CALM_L2 = {
    "allowed": True,
    "risk_multiplier": 1.0,
    "state": "calm",
    "direction_support": "",
}


def _trend_rows(direction: int, count: int = 220) -> list[dict]:
    rows = []
    price = 100.0
    for index in range(count):
        step = (0.18 * direction) + (0.04 if index % 5 == 0 else -0.02)
        open_price = price
        price = max(5.0, price + step)
        rows.append(
            {
                "timestamp": index * 3_600_000,
                "open": open_price,
                "high": max(open_price, price) + 0.12,
                "low": min(open_price, price) - 0.12,
                "close": price,
                "volume": 1_000.0 + index,
            }
        )
    rows[-1]["close"] += 0.50 * direction
    if direction > 0:
        rows[-1]["high"] = rows[-1]["close"] + 0.10
    else:
        rows[-1]["low"] = rows[-1]["close"] - 0.10
    return rows


@pytest.mark.parametrize(("direction", "side"), ((1, "long"), (-1, "short")))
def test_multi_horizon_breakout_accepts_both_directions(direction, side):
    decision = evaluate_adaptive_breakout_trend(
        _trend_rows(direction),
        CALM_L2,
        {"breakout_entry_enabled": True},
    )

    assert decision.allowed is True
    assert decision.side == side
    assert decision.score >= default_adaptive_breakout_trend_config()["score_min"]
    assert 0.60 <= decision.risk_multiplier <= 1.0
    assert decision.metrics["fresh_breakout"] is True
    assert sum(value == side for value in decision.metrics["horizon_votes"].values()) >= 2


def _ema_crossover_rows(direction: int, count: int = 240) -> list[dict]:
    rows = []
    price = 100.0
    for index in range(count):
        if index < count - 32:
            step = 0.12 * direction
        elif index < count - 8:
            step = -0.18 * direction
        else:
            step = 0.50 * direction
        open_price = price
        price = max(5.0, price + step)
        rows.append(
            {
                "timestamp": index * 3_600_000,
                "open": open_price,
                "high": max(open_price, price) + 0.10,
                "low": min(open_price, price) - 0.10,
                "close": price,
                "volume": 1_000.0 + index,
            }
        )
    return rows


@pytest.mark.parametrize(("direction", "side"), ((1, "long"), (-1, "short")))
def test_recent_ema_crossover_is_the_default_entry_trigger(direction, side):
    rows = _ema_crossover_rows(direction)
    decision = evaluate_adaptive_breakout_trend(rows, CALM_L2)

    assert decision.allowed is True
    assert decision.side == side
    assert decision.metrics["ema_crossover"] is True
    assert decision.metrics["ema_crossover_age_bars"] in {0, 1, 2}
    assert decision.metrics["signal_candle_ts"] != rows[-1]["timestamp"] or (
        decision.metrics["ema_crossover_age_bars"] == 0
    )


def test_old_extended_trend_waits_for_weighted_reacceleration_by_default():
    decision = evaluate_adaptive_breakout_trend(_trend_rows(1), CALM_L2)

    assert decision.allowed is False
    assert decision.side == "long"
    assert decision.reason == "waiting_for_weighted_trend_entry"
    assert decision.metrics["breakout_entry_enabled"] is False
    assert decision.metrics["fresh_breakout"] is False
    assert decision.metrics["reacceleration"] is False


@pytest.mark.parametrize(("direction", "side"), ((1, "long"), (-1, "short")))
def test_weighted_continuation_enters_without_requiring_a_new_crossover(direction, side):
    decision = evaluate_adaptive_breakout_trend(
        _trend_rows(direction),
        CALM_L2,
        {
            "profile_version": "adaptive_trend_portfolio_v4_small_account_no_daily_loss",
            "continuation_max_fast_ema_distance_atr": 4.0,
        },
    )

    assert decision.allowed is True
    assert decision.side == side
    assert decision.metrics["ema_crossover"] is False
    assert decision.metrics["weighted_continuation"] is True
    assert "weighted continuation" in decision.reason


def test_crossover_floor_cannot_drop_below_half_the_broad_momentum_floor():
    decision = evaluate_adaptive_breakout_trend(
        _ema_crossover_rows(1),
        CALM_L2,
        {
            "minimum_momentum_strength": 0.99,
            "ema_crossover_minimum_momentum_strength": 0.0,
        },
    )

    assert decision.allowed is True
    assert decision.metrics["ema_crossover"] is True
    assert decision.metrics["minimum_momentum_strength_required"] == pytest.approx(0.495)


def test_crossover_window_keeps_one_stable_signal_timestamp():
    rows = _ema_crossover_rows(1)
    current = evaluate_adaptive_breakout_trend(rows[:-1], CALM_L2)
    following = evaluate_adaptive_breakout_trend(rows, CALM_L2)

    assert current.allowed is True
    assert following.allowed is True
    assert current.metrics["signal_candle_ts"] == following.metrics["signal_candle_ts"]


def test_status_labels_the_ema_crossover_entry_mode():
    status_source = (ROOT / "bot_runtime" / "signal_alpha.py").read_text(
        encoding="utf-8"
    )

    assert "EMA crossover (" in status_source
    assert "metrics.get('ema_crossover_age_bars'" in status_source


def test_live_crossover_reuses_completed_candle_and_keeps_decision_metadata(monkeypatch):
    rows = _ema_crossover_rows(1)
    current_time_module = __import__("time")
    evaluation_now_ms = int(current_time_module.time() * 1000.0)
    current_candle_open_ms = evaluation_now_ms - (evaluation_now_ms % 3_600_000)
    timestamp_shift = current_candle_open_ms - rows[-1]["timestamp"]
    for row in rows:
        row["timestamp"] += timestamp_shift
    ohlcv = [
        [
            row["timestamp"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row["volume"],
        ]
        for row in rows
    ]
    fetch_count = 0

    class MarketData:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            nonlocal fetch_count
            fetch_count += 1
            return ohlcv

        def fetch_ticker(self, symbol):
            return {"last": rows[-1]["close"]}

    engine = SignalAlphaMixin()
    engine.market_data_exchange = MarketData()
    engine.last_entry_reason = {}
    engine.adaptive_breakout_trend_last_status = {}
    engine._canonical_futures_symbol = lambda symbol: symbol
    engine._clear_utbot_filtered_breakout_entry_plan = lambda symbol: None
    engine._timeframe_to_ms = lambda timeframe: 3_600_000
    engine._relative_strength_pullback_rows_from_ohlcv = lambda values: [
        {
            "timestamp": value[0],
            "open": value[1],
            "high": value[2],
            "low": value[3],
            "close": value[4],
            "volume": value[5],
        }
        for value in values
    ]
    engine._get_utbot_filtered_breakout_config = lambda params: {
        "adaptive_breakout_trend": {
            "enabled": True,
            "live_enabled": True,
            "ema_crossover_minimum_momentum_strength": 0.0,
            "entry_chase_max_atr": 2.0,
        },
        "daily_max_loss_usdt": 100.0,
        "max_daily_trades": 5,
    }
    engine.is_upbit_mode = lambda: False
    engine.is_trade_direction_allowed = lambda side: True
    engine.db = SimpleNamespace(get_daily_stats=lambda: (0, 0.0))
    engine.get_automatic_daily_entry_count = lambda: 0
    engine.get_balance_info = lambda: asyncio.sleep(0, result=(100.0, 100.0, 0.0))
    engine.get_runtime_common_settings = lambda: {"leverage": 5}
    engine._evaluate_utbreakout_market_quality = lambda side, cfg, values: {
        "hard_block": False,
        "state": True,
        "risk_multiplier": 1.0,
        "summary": "ok",
    }
    engine._evaluate_shared_l2_gate = lambda *args, **kwargs: asyncio.sleep(
        0,
        result=dict(CALM_L2),
    )
    stored_plans = {}
    stored_statuses = {}
    engine._set_utbot_filtered_breakout_entry_plan = (
        lambda symbol, plan: stored_plans.__setitem__(symbol, dict(plan))
    )
    engine._store_utbot_filtered_breakout_status = (
        lambda symbol, status: stored_statuses.__setitem__(symbol, dict(status))
    )

    monkeypatch.setattr(signal_alpha_module, "asyncio", asyncio, raising=False)
    monkeypatch.setattr(
        signal_alpha_module,
        "time",
        current_time_module,
        raising=False,
    )
    monkeypatch.setattr(
        signal_alpha_module,
        "ADAPTIVE_BREAKOUT_TREND_STRATEGY",
        ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        raising=False,
    )
    monkeypatch.setattr(
        signal_alpha_module,
        "STRATEGY_DISPLAY_NAMES",
        {ADAPTIVE_BREAKOUT_TREND_STRATEGY: "Adaptive Breakout Trend"},
        raising=False,
    )
    monkeypatch.setattr(
        signal_alpha_module,
        "evaluate_adaptive_breakout_trend",
        evaluate_adaptive_breakout_trend,
        raising=False,
    )
    monkeypatch.setattr(
        signal_alpha_module,
        "completed_candle_rows",
        completed_candle_rows,
        raising=False,
    )
    monkeypatch.setattr(
        signal_alpha_module,
        "_safe_float_or_none",
        _safe_float_or_none,
        raising=False,
    )
    monkeypatch.setattr(
        signal_alpha_module,
        "resolve_utbreakout_risk_budget",
        resolve_utbreakout_risk_budget,
        raising=False,
    )
    monkeypatch.setattr(
        signal_alpha_module,
        "calculate_risk_plan",
        calculate_risk_plan,
        raising=False,
    )
    monkeypatch.setattr(
        signal_alpha_module,
        "cap_utbreakout_risk_plan_to_margin",
        cap_utbreakout_risk_plan_to_margin,
        raising=False,
    )

    first = asyncio.run(
        engine._calculate_adaptive_breakout_trend_signal(
            "TEST/USDT:USDT",
            None,
            {},
            force_reprocess=True,
        )
    )
    second = asyncio.run(
        engine._calculate_adaptive_breakout_trend_signal(
            "TEST/USDT:USDT",
            None,
            {},
            force_reprocess=True,
        )
    )

    assert first[0] == "long", first
    assert second[0] == "long", second
    plan = stored_plans["TEST/USDT:USDT"]
    status = stored_statuses["TEST/USDT:USDT"]
    assert fetch_count == 2
    assert status["candle_cache_hit"] is True
    assert status["entry_timeframe"] == "1h"
    assert status["completed_candle_ts"] == rows[-2]["timestamp"]
    assert status["decision_candle_ts"] == plan["signal_candle_ts"]
    assert status["decision_candle_ts"] < current_candle_open_ms
    assert plan["risk_budget_mode"] == "adaptive_trend_small_account_aggressive_pending"
    assert plan["adaptive_breakout_trend_target_risk_percent"] >= 1.5
    assert plan["adaptive_trend_initial_fraction"] == pytest.approx(0.65)
    assert plan["adaptive_trend_target_qty"] > plan["qty"]
    assert plan["partial_take_profit_ratio"] == pytest.approx(0.15)
    assert plan["second_take_profit_enabled"] is False
    assert plan["runner_pct"] == pytest.approx(0.85)

    # A same-timestamp exchange correction must invalidate the preliminary
    # cache instead of freezing the first snapshot for the rest of the hour.
    ohlcv[-2][5] += 1.0
    third = asyncio.run(
        engine._calculate_adaptive_breakout_trend_signal(
            "TEST/USDT:USDT",
            None,
            {},
            force_reprocess=True,
        )
    )
    assert third[0] == "long", third
    assert fetch_count == 3
    assert stored_statuses["TEST/USDT:USDT"]["candle_cache_hit"] is False


def test_l2_stress_is_a_hard_safety_gate():
    decision = evaluate_adaptive_breakout_trend(
        _trend_rows(1),
        {"allowed": False, "risk_multiplier": 0.0, "state": "stressed_thin"},
        {"breakout_entry_enabled": True},
    )

    assert decision.allowed is False
    assert decision.side == "long"
    assert decision.reason == "l2_stressed"


def test_volatility_shock_is_rejected_before_sizing():
    cfg = {"volatility_shock_ratio": 1.01, "breakout_entry_enabled": True}
    decision = evaluate_adaptive_breakout_trend(_trend_rows(1), CALM_L2, cfg)

    assert decision.allowed is False
    assert decision.reason == "volatility_shock"


def test_horizon_normalization_preserves_unsorted_weight_mapping():
    horizons, weights = _normalized_momentum_horizons(
        (168, 24, 72),
        (0.6, 0.1, 0.3),
    )

    assert horizons == (24, 72, 168)
    assert weights == pytest.approx((0.1, 0.3, 0.6))


def test_empty_horizon_config_falls_back_without_crashing():
    decision = evaluate_adaptive_breakout_trend(
        _trend_rows(1),
        CALM_L2,
        {
            "momentum_horizons": (),
            "momentum_weights": (),
            "breakout_entry_enabled": True,
        },
    )

    assert decision.allowed is True
    assert decision.side == "long"


def test_trend_universe_config_normalizes_and_preserves_fail_closed_single_mode():
    single = normalize_adaptive_breakout_trend_config(
        {"universe_mode": " SINGLE ", "single_symbol": " btc/usdt:usdt "}
    )
    invalid = normalize_adaptive_breakout_trend_config(
        {"universe_mode": "unexpected", "single_symbol": "ETH/USDT:USDT"}
    )
    empty_single = normalize_adaptive_breakout_trend_config(
        {"universe_mode": "single", "single_symbol": ""}
    )

    assert single["universe_mode"] == "single"
    assert single["single_symbol"] == "BTC/USDT:USDT"
    assert invalid["universe_mode"] == "auto"
    assert empty_single["universe_mode"] == "single"


def test_crossover_momentum_floor_is_relative_and_malformed_values_fall_back():
    guarded = normalize_adaptive_breakout_trend_config(
        {
            "minimum_momentum_strength": 0.18,
            "ema_crossover_minimum_momentum_strength": 0.01,
            "ema_crossover_momentum_floor_ratio": 0.10,
        }
    )
    malformed = normalize_adaptive_breakout_trend_config(
        {
            "minimum_momentum_strength": None,
            "ema_crossover_minimum_momentum_strength": "invalid",
            "ema_crossover_momentum_floor_ratio": None,
        }
    )

    assert guarded["ema_crossover_momentum_floor_ratio"] == pytest.approx(0.50)
    assert guarded["ema_crossover_minimum_momentum_strength"] == pytest.approx(0.09)
    assert malformed["minimum_momentum_strength"] == pytest.approx(0.18)
    assert malformed["ema_crossover_minimum_momentum_strength"] == pytest.approx(0.09)


def test_persisted_conservative_profile_migrates_to_small_account_aggressive_v3():
    migrated = normalize_adaptive_breakout_trend_config(
        {
            "universe_mode": "single",
            "single_symbol": "BTC/USDT:USDT",
            "take_profit_r_multiple": 4.0,
            "base_risk_multiplier": 0.8,
            "runner_pct": 0.60,
        }
    )

    assert migrated["profile_version"] == "adaptive_trend_portfolio_v4_small_account_no_daily_loss"
    assert migrated["universe_mode"] == "single"
    assert migrated["single_symbol"] == "BTC/USDT:USDT"
    assert migrated["take_profit_r_multiple"] == pytest.approx(10.0)
    assert migrated["base_risk_percent"] == pytest.approx(1.75)
    assert migrated["runner_pct"] == pytest.approx(0.85)
    assert migrated["small_account_margin_budget_fraction"] == pytest.approx(0.95)
    assert migrated["small_account_initial_margin_fraction"] == pytest.approx(0.65)
    assert migrated["small_account_base_max_loss_percent"] == pytest.approx(20.0)
    assert migrated["small_account_strong_max_loss_percent"] == pytest.approx(30.0)
    assert migrated["small_account_elite_max_loss_percent"] == pytest.approx(35.0)
    assert migrated["small_account_daily_loss_limit_percent"] == pytest.approx(0.0)


def test_trend_single_universe_resolves_one_symbol_and_invalid_symbol_fails_closed():
    scanner = SignalScannerMixin()
    scanner._get_utbot_filtered_breakout_config = (
        lambda params: params["UTBotFilteredBreakoutV1"]
    )
    trade_cfg = {
        "strategy_params": {
            "active_strategy": ADAPTIVE_BREAKOUT_TREND_STRATEGY,
            "UTBotFilteredBreakoutV1": {
                "adaptive_breakout_trend": {
                    "universe_mode": "single",
                    "single_symbol": "ETH/USDT:USDT",
                }
            },
        }
    }
    scanner._ensure_valid_utbreakout_market_symbol = (
        lambda symbol, source: (True, symbol, None)
    )

    resolved = scanner._resolve_adaptive_trend_scan_universe(trade_cfg)

    assert resolved == {
        "single_mode": True,
        "symbol": "ETH/USDT:USDT",
        "reason": None,
    }

    scanner._ensure_valid_utbreakout_market_symbol = (
        lambda symbol, source: (False, symbol, "REJECTED_INVALID_MARKET_SYMBOL")
    )
    rejected = scanner._resolve_adaptive_trend_scan_universe(trade_cfg)

    assert rejected["single_mode"] is True
    assert rejected["symbol"] is None
    assert rejected["reason"] == "REJECTED_INVALID_MARKET_SYMBOL"


def test_strong_standalone_signal_can_reach_high_dynamic_leverage():
    plan = {
        "strategy": ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        "side": "long",
        "entry_price": 100.0,
        "risk_distance": 1.0,
        "atr": 1.0,
        "adaptive_breakout_trend_score": 95.0,
        "market_quality_risk_multiplier": 1.0,
        "l2_gate": CALM_L2,
        "l2_state": "calm",
        "l2_risk_multiplier": 1.0,
    }

    decision = select_dynamic_leverage(plan)

    assert decision.quality_source == "adaptive_breakout_trend_score"
    assert decision.tier == "adaptive_trend_elite"
    assert decision.leverage == 15


def test_dedicated_telegram_mode_is_separate_and_mutually_exclusive():
    setup_source = (ROOT / "bot_runtime" / "controller_telegram_setup.py").read_text(
        encoding="utf-8"
    )
    keyboard_source = (ROOT / "bot_runtime" / "controller_telegram.py").read_text(
        encoding="utf-8"
    )

    assert 'KeyboardButton("/trend")' in keyboard_source
    assert "callback_data='trend:on'" in setup_source
    assert "callback_data='trend:off'" in setup_source
    assert "callback_data='trend:status'" in setup_source
    assert "callback_data='trend:single'" in setup_source
    assert "callback_data='trend:auto'" in setup_source
    assert "adaptive_trend_waiting_for_symbol" in setup_source
    assert "'quad_alpha_enabled_strategies'],\n                []," in setup_source
    assert "reset_exit_cache=False" in setup_source
    assert "reply_markup=self._build_main_keyboard()" in setup_source


def test_status_explains_testnet_tradfi_scope_block_instead_of_candle_wait():
    engine = SignalAlphaMixin()
    engine.current_utbreakout_candidate_symbol = None
    engine.adaptive_breakout_trend_last_status = {}
    engine.coin_selector_last_result = {}
    engine._canonical_futures_symbol = lambda symbol: symbol
    engine.get_automatic_scan_scope = lambda: "tradfi_only"
    engine.ctrl = SimpleNamespace(get_exchange_mode=lambda: "binance_testnet")

    text = asyncio.run(engine.build_adaptive_breakout_trend_status_text())

    assert "스캐너 차단" in text
    assert "완료된 1시간봉 평가 기록" not in text


def test_status_reports_empty_selector_rejections():
    engine = SignalAlphaMixin()
    engine.current_utbreakout_candidate_symbol = None
    engine.adaptive_breakout_trend_last_status = {}
    engine.coin_selector_last_result = {
        "selected": [],
        "watch_only": [],
        "reject_counts": {"REJECTED_MIN_VOLUME": 12},
    }
    engine._canonical_futures_symbol = lambda symbol: symbol
    engine.get_automatic_scan_scope = lambda: "crypto_only"
    engine.ctrl = SimpleNamespace(get_exchange_mode=lambda: "binance_testnet")

    text = asyncio.run(engine.build_adaptive_breakout_trend_status_text())

    assert "스캐너 후보 없음" in text
    assert "REJECTED_MIN_VOLUME=12" in text


def test_status_uses_configured_trend_single_symbol_before_scanner_candidate():
    engine = SignalAlphaMixin()
    engine.current_utbreakout_candidate_symbol = "BTC/USDT:USDT"
    engine.adaptive_breakout_trend_last_status = {}
    engine.coin_selector_last_result = {}
    engine._canonical_futures_symbol = lambda symbol: symbol
    engine.get_runtime_strategy_params = lambda: {
        "UTBotFilteredBreakoutV1": {
            "adaptive_breakout_trend": {
                "universe_mode": "single",
                "single_symbol": "ETH/USDT:USDT",
            }
        }
    }
    engine.get_automatic_scan_scope = lambda: "crypto_only"
    engine.ctrl = SimpleNamespace(get_exchange_mode=lambda: "binance_testnet")

    text = asyncio.run(engine.build_adaptive_breakout_trend_status_text())

    assert "Symbol: ETH/USDT:USDT" in text
    assert "Universe: single / ETH/USDT:USDT" in text


def test_status_explains_no_entry_reason_in_korean():
    engine = SignalAlphaMixin()
    engine.current_utbreakout_candidate_symbol = "PROM/USDT:USDT"
    engine.adaptive_breakout_trend_last_status = {
        "PROM/USDT:USDT": {
            "strategy": "ADAPTIVE_BREAKOUT_TREND",
            "symbol": "PROM/USDT:USDT",
            "stage": "waiting",
            "allowed": False,
            "side": "long",
            "score": 0.0,
            "reason": "Adaptive Breakout Trend waiting: momentum_strength_too_low",
            "metrics": {},
        }
    }
    engine._canonical_futures_symbol = lambda symbol: symbol
    engine.get_automatic_scan_scope = lambda: "crypto_only"
    engine._utbreakout_recent_trace_events = lambda symbol, limit=80: [
        {
            "ts": 100.0,
            "symbol": "PROM/USDT:USDT",
            "stage": "SIGNAL_CALCULATED",
            "status": "RESULT",
            "data": {
                "reason": (
                    "Adaptive Breakout Trend waiting: momentum_strength_too_low"
                )
            },
        },
        {
            "ts": 101.0,
            "symbol": "PROM/USDT:USDT",
            "stage": "AUTO_ENTRY_BRIDGE_BLOCKED",
            "status": "NO_STATUS_READY",
            "data": {
                "reason": (
                    "no live STATUS_READY and no accepted diagnostic/plan"
                )
            },
        },
    ]

    text = asyncio.run(engine.build_adaptive_breakout_trend_status_text())

    assert "진입하지 않은 이유:" in text
    assert "모멘텀이 약해" in text


def test_durable_entry_summary_keeps_restart_exit_policy():
    plan = {
        "strategy": ADAPTIVE_BREAKOUT_TREND_STRATEGY,
        "runner_chandelier_lookback": 48,
        "runner_structure_lookback": 12,
        "dynamic_leverage_monitor_timeframe": "15m",
        "ev_time_stop_enabled": True,
        "ev_time_stop_bars": 16,
        "ev_time_stop_min_mfe_r": 0.35,
        "ev_time_stop_max_current_r": 0.0,
        "ignored_runtime_object": object(),
    }

    summary = build_durable_entry_plan_summary(plan)

    assert summary["runner_chandelier_lookback"] == 48
    assert summary["runner_structure_lookback"] == 12
    assert summary["dynamic_leverage_monitor_timeframe"] == "15m"
    assert summary["ev_time_stop_enabled"] is True
    assert summary["ev_time_stop_bars"] == 16
    assert summary["ev_time_stop_min_mfe_r"] == pytest.approx(0.35)
    assert summary["ev_time_stop_max_current_r"] == pytest.approx(0.0)
    assert "ignored_runtime_object" not in summary


def test_dispatch_and_registry_include_standalone_strategy():
    candle_source = (ROOT / "bot_runtime" / "signal_candles.py").read_text(
        encoding="utf-8"
    )
    registry_source = (ROOT / "bot_runtime" / "strategy_registry.py").read_text(
        encoding="utf-8"
    )

    assert "active_strategy == ADAPTIVE_BREAKOUT_TREND_STRATEGY" in candle_source
    assert "_calculate_adaptive_breakout_trend_signal" in candle_source
    assert "ADAPTIVE_BREAKOUT_TREND_STRATEGY," in registry_source

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_old_trend_without_recent_crossover_does_not_chase_channel_by_default():
    decision = evaluate_adaptive_breakout_trend(_trend_rows(1), CALM_L2)

    assert decision.allowed is False
    assert decision.side == "long"
    assert decision.reason == "waiting_for_ema_crossover"


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
    assert decision.leverage == 10


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

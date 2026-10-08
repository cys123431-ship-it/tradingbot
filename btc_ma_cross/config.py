"""Configuration for the BTC 3/200 SMA cross strategy (stop-and-reverse).

OFF and DRY_RUN by default.  Leverage is fixed at 5x and each entry uses
50% of the available futures balance as margin.  The emergency stop is
selectable from Telegram (OFF or a margin-ROI loss).
"""

from __future__ import annotations

from copy import deepcopy

from btc_ema_pullback.config import (
    NETWORK_TESTNET,
    NETWORKS,
    TRADING_MODE_DRY_RUN,
    TRADING_MODES,
    _bool,
    _decimal_text,
    _float,
    _int,
)

STRATEGY_NAME = "btc_sma3_sma200_cross"
STRATEGY_VERSION = "1.0.0"
CLIENT_ID_STRATEGY = "btcma"
CONFIG_KEY = "btc_ma_cross"

TIMEFRAMES = ("15m", "30m", "1h", "2h", "4h")
# Margin-ROI loss that triggers the emergency stop; 0 = OFF.
EMERGENCY_STOP_CHOICES = (0, 10, 20, 30, 50)


def default_btc_ma_cross_config() -> dict:
    return {
        "enabled": False,
        "trading_mode": TRADING_MODE_DRY_RUN,
        # The live network follows the bot's /setup exchange mode; this
        # value is only a fallback for offline tests/backtests.
        "network": NETWORK_TESTNET,
        "symbol": "BTCUSDT",
        "strategy_name": STRATEGY_NAME,
        "timeframe": "1h",
        "fast_period": 3,
        "slow_period": 200,
        "candle_history": 260,
        "leverage": 5,
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        # Share of the available futures balance used as margin per entry.
        "margin_fraction": "0.5",
        "emergency_stop_roi_percent": 30,
        # Profit lock (margin ROI): at +5% keep +4%, then every +5% one more
        # step, always lock_gap below the step reached.
        "lock_start_roi_percent": "5",
        "lock_step_percent": "5",
        "lock_gap_percent": "1",
        # Emergency stop on mark price (no wick stop-outs); the profit lock on
        # the last price so the market fill lands near the locked level.
        "working_type": "MARK_PRICE",
        "lock_working_type": "CONTRACT_PRICE",
        "tp_working_type": "CONTRACT_PRICE",
        "signal_max_age_seconds": 180,
        "estimated_taker_fee_rate": "0.0005",
        "slippage_buffer_pct": "0.0005",
        "max_margin_usage_pct": "0.95",
        "protection_retry_attempts": 3,
        "protection_retry_delays_seconds": [0.5, 1.0, 2.0],
        "protection_check_interval_seconds": 60,
        "loop_interval_seconds": 10,
        "rules_refresh_seconds": 3600,
        "dry_run_balance_usdt": "100",
        "day_timezone": "Asia/Seoul",
        "allow_auto_raise_to_exchange_minimum": False,
    }


def normalize_btc_ma_cross_config(raw=None) -> dict:
    supplied = dict(raw) if isinstance(raw, dict) else {}
    cfg = deepcopy(default_btc_ma_cross_config())
    cfg.update(supplied)
    defaults = default_btc_ma_cross_config()
    cfg["enabled"] = _bool(cfg.get("enabled"), False)
    mode = str(cfg.get("trading_mode") or TRADING_MODE_DRY_RUN).strip().upper()
    cfg["trading_mode"] = mode if mode in TRADING_MODES else TRADING_MODE_DRY_RUN
    network = str(cfg.get("network") or NETWORK_TESTNET).strip().lower()
    cfg["network"] = network if network in NETWORKS else NETWORK_TESTNET
    cfg["symbol"] = "BTCUSDT"
    cfg["strategy_name"] = STRATEGY_NAME
    timeframe = str(cfg.get("timeframe") or "1h").strip().lower()
    cfg["timeframe"] = timeframe if timeframe in TIMEFRAMES else "1h"
    cfg["fast_period"] = 3
    cfg["slow_period"] = 200
    cfg["candle_history"] = _int(cfg.get("candle_history"), 260, 210, 1000)
    cfg["leverage"] = 5  # fixed by the strategy definition
    cfg["margin_type"] = "ISOLATED"
    cfg["position_mode"] = "ONE_WAY"
    cfg["margin_fraction"] = _decimal_text(cfg.get("margin_fraction"), "0.5", 0.05, 0.95)
    try:
        stop = int(float(cfg.get("emergency_stop_roi_percent")))
    except (TypeError, ValueError):
        stop = defaults["emergency_stop_roi_percent"]
    cfg["emergency_stop_roi_percent"] = stop if stop in EMERGENCY_STOP_CHOICES else defaults["emergency_stop_roi_percent"]
    cfg["lock_start_roi_percent"] = _decimal_text(cfg.get("lock_start_roi_percent"), "5", 1.0, 100.0)
    cfg["lock_step_percent"] = _decimal_text(cfg.get("lock_step_percent"), "5", 1.0, 50.0)
    cfg["lock_gap_percent"] = _decimal_text(cfg.get("lock_gap_percent"), "1", 0.1, float(cfg["lock_start_roi_percent"]))
    for key, default in (("working_type", "MARK_PRICE"), ("lock_working_type", "CONTRACT_PRICE"),
                         ("tp_working_type", "CONTRACT_PRICE")):
        value = str(cfg.get(key) or default).strip().upper()
        cfg[key] = value if value in {"MARK_PRICE", "CONTRACT_PRICE"} else default
    cfg["signal_max_age_seconds"] = _int(cfg.get("signal_max_age_seconds"), 180, 30, 900)
    cfg["estimated_taker_fee_rate"] = _decimal_text(cfg.get("estimated_taker_fee_rate"), "0.0005", 0.0, 0.005)
    cfg["slippage_buffer_pct"] = _decimal_text(cfg.get("slippage_buffer_pct"), "0.0005", 0.0, 0.01)
    cfg["max_margin_usage_pct"] = _decimal_text(cfg.get("max_margin_usage_pct"), "0.95", 0.1, 1.0)
    cfg["protection_retry_attempts"] = _int(cfg.get("protection_retry_attempts"), 3, 1, 6)
    delays = cfg.get("protection_retry_delays_seconds")
    if not isinstance(delays, (list, tuple)) or not delays:
        delays = defaults["protection_retry_delays_seconds"]
    cfg["protection_retry_delays_seconds"] = [_float(v, 1.0, 0.0, 10.0) for v in list(delays)[:6]]
    cfg["protection_check_interval_seconds"] = _int(cfg.get("protection_check_interval_seconds"), 60, 10, 600)
    cfg["loop_interval_seconds"] = _int(cfg.get("loop_interval_seconds"), 10, 5, 60)
    cfg["rules_refresh_seconds"] = _int(cfg.get("rules_refresh_seconds"), 3600, 300, 86400)
    cfg["dry_run_balance_usdt"] = _decimal_text(cfg.get("dry_run_balance_usdt"), "100", 10.0, 10_000_000.0)
    cfg["day_timezone"] = str(cfg.get("day_timezone") or "Asia/Seoul")
    cfg["allow_auto_raise_to_exchange_minimum"] = _bool(cfg.get("allow_auto_raise_to_exchange_minimum"), False)
    return cfg


__all__ = (
    "CLIENT_ID_STRATEGY",
    "CONFIG_KEY",
    "EMERGENCY_STOP_CHOICES",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "TIMEFRAMES",
    "default_btc_ma_cross_config",
    "normalize_btc_ma_cross_config",
)

"""Configuration contract for the BTC EMA pullback (1h trend + 15m pullback) strategy.

Every tunable number of the strategy lives here.  The strategy is OFF and in
DRY_RUN by default; LIVE needs an explicit config/Telegram change and is
refused while the environment variable ``BTC_PULLBACK_TRADING_MODE`` forces
``DRY_RUN``.
"""

from __future__ import annotations

import os
from copy import deepcopy

STRATEGY_NAME = "btc_ema_pullback_small_account"
STRATEGY_VERSION = "1.0.0"
# Short prefix for deterministic clientOrderId / clientAlgoId values.
CLIENT_ID_STRATEGY = "btcpb"

TRADING_MODE_DRY_RUN = "DRY_RUN"
TRADING_MODE_LIVE = "LIVE"
TRADING_MODES = (TRADING_MODE_DRY_RUN, TRADING_MODE_LIVE)
TRADING_MODE_ENV = "BTC_PULLBACK_TRADING_MODE"

NETWORK_TESTNET = "testnet"
NETWORK_MAINNET = "mainnet"
NETWORKS = (NETWORK_TESTNET, NETWORK_MAINNET)

SIZING_FIXED = "fixed"
SIZING_RISK_BASED = "risk_based"
SIZING_MODES = (SIZING_FIXED, SIZING_RISK_BASED)


def default_btc_pullback_config() -> dict:
    return {
        "enabled": False,
        "trading_mode": TRADING_MODE_DRY_RUN,
        # The live network follows the bot's /setup exchange mode; this value
        # is only a fallback for offline tests/backtests.
        "network": NETWORK_TESTNET,
        "symbol": "BTCUSDT",
        "strategy_name": STRATEGY_NAME,
        "leverage": 3,
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "base_quantity": "0.001",
        # Mainnet keeps the small-account fixed 0.001 BTC rule.  On testnet the
        # small-account cap is lifted: size from the 0.5% risk budget instead.
        "sizing_mode_mainnet": SIZING_FIXED,
        "sizing_mode_testnet": SIZING_RISK_BASED,
        "ema_fast": 20,
        "ema_slow": 50,
        "trend_timeframe": "1h",
        "entry_timeframe": "15m",
        "candle_history": 220,
        "stop_loss_pct": "0.008",
        "take_profit_pct": "0.016",
        "max_risk_per_trade_pct": "0.005",
        "max_daily_loss_pct": "0.015",
        "max_trades_per_day": 3,
        "max_consecutive_losses": 3,
        "day_timezone": "Asia/Seoul",
        # --- sideways (chop) filter on the 1h trend frame ---
        # |EMA20 - EMA50| / close must be at least this (0.15%).
        "ema_separation_threshold": "0.0015",
        # |EMA20 change| per 1h bar averaged over ``ema_slope_lookback`` bars,
        # as a fraction of price, must be at least this (0.02% per hour).
        "ema_slope_threshold": "0.0002",
        "ema_slope_lookback": 3,
        "recent_cross_lookback": 24,
        "max_recent_crosses": 1,
        # --- 15m pullback / confirmation ---
        "pullback_lookback": 4,
        # A low within 0.10% above EMA20 (LONG) counts as touching EMA20.
        "pullback_tolerance_pct": "0.001",
        # A close more than 0.10% beyond EMA50 counts as a clear break.
        "ema50_break_tolerance_pct": "0.001",
        "atr_period": 14,
        # Anti-chase: the confirmation candle range / body vs ATR(14).
        "max_entry_candle_atr_multiple": "1.8",
        "max_entry_body_atr_multiple": "1.2",
        # A signal older than this (after the 15m close) is not traded.
        "signal_max_age_seconds": 180,
        # --- costs ---
        "estimated_taker_fee_rate": "0.0005",
        "estimated_maker_fee_rate": "0.0002",
        "slippage_buffer_pct": "0.0005",
        # Never use more than this share of available balance as margin.
        "max_margin_usage_pct": "0.9",
        # --- protection orders ---
        # Stop on MARK_PRICE (no wick stop-outs); take profit on the last
        # traded price so the market fill lands near the target.
        "working_type": "MARK_PRICE",
        "tp_working_type": "CONTRACT_PRICE",
        "protection_retry_attempts": 3,
        "protection_retry_delays_seconds": [0.5, 1.0, 2.0],
        "protection_check_interval_seconds": 60,
        # --- loop ---
        "loop_interval_seconds": 10,
        "rules_refresh_seconds": 3600,
        # --- DRY_RUN paper account (used when no API key is configured) ---
        "dry_run_balance_usdt": "210",
        # --- hard prohibitions (cannot be enabled) ---
        "allow_pyramiding": False,
        "allow_martingale": False,
        "allow_averaging": False,
        "allow_auto_raise_to_exchange_minimum": False,
    }


def _float(value, default, low, high):
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if result != result:  # NaN
        result = float(default)
    return min(float(high), max(float(low), result))


def _int(value, default, low, high):
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = int(default)
    return min(int(high), max(int(low), result))


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _decimal_text(value, default, low, high):
    return format(_float(value, default, low, high), ".10g")


def normalize_btc_pullback_config(raw=None) -> dict:
    supplied = dict(raw) if isinstance(raw, dict) else {}
    cfg = deepcopy(default_btc_pullback_config())
    cfg.update(supplied)
    defaults = default_btc_pullback_config()

    cfg["enabled"] = _bool(cfg.get("enabled"), False)
    mode = str(cfg.get("trading_mode") or TRADING_MODE_DRY_RUN).strip().upper()
    cfg["trading_mode"] = mode if mode in TRADING_MODES else TRADING_MODE_DRY_RUN
    network = str(cfg.get("network") or NETWORK_TESTNET).strip().lower()
    cfg["network"] = network if network in NETWORKS else NETWORK_TESTNET
    # This strategy is BTCUSDT-only by design.
    cfg["symbol"] = "BTCUSDT"
    cfg["strategy_name"] = STRATEGY_NAME
    cfg["leverage"] = _int(cfg.get("leverage"), 3, 1, 3)
    cfg["margin_type"] = "ISOLATED"
    cfg["position_mode"] = "ONE_WAY"
    cfg["base_quantity"] = _decimal_text(cfg.get("base_quantity"), defaults["base_quantity"], 0.0001, 1.0)
    for key in ("sizing_mode_mainnet", "sizing_mode_testnet"):
        value = str(cfg.get(key) or defaults[key]).strip().lower()
        cfg[key] = value if value in SIZING_MODES else defaults[key]

    cfg["ema_fast"] = _int(cfg.get("ema_fast"), 20, 2, 200)
    cfg["ema_slow"] = _int(cfg.get("ema_slow"), 50, 3, 400)
    if cfg["ema_slow"] <= cfg["ema_fast"]:
        cfg["ema_slow"] = cfg["ema_fast"] + 1
    cfg["trend_timeframe"] = "1h"
    cfg["entry_timeframe"] = "15m"
    cfg["candle_history"] = _int(cfg.get("candle_history"), 220, cfg["ema_slow"] * 3, 1000)

    cfg["stop_loss_pct"] = _decimal_text(cfg.get("stop_loss_pct"), "0.008", 0.001, 0.05)
    cfg["take_profit_pct"] = _decimal_text(cfg.get("take_profit_pct"), "0.016", 0.001, 0.2)
    cfg["max_risk_per_trade_pct"] = _decimal_text(cfg.get("max_risk_per_trade_pct"), "0.005", 0.0005, 0.02)
    cfg["max_daily_loss_pct"] = _decimal_text(cfg.get("max_daily_loss_pct"), "0.015", 0.002, 0.1)
    cfg["max_trades_per_day"] = _int(cfg.get("max_trades_per_day"), 3, 1, 20)
    cfg["max_consecutive_losses"] = _int(cfg.get("max_consecutive_losses"), 3, 1, 20)
    cfg["day_timezone"] = str(cfg.get("day_timezone") or "Asia/Seoul")

    cfg["ema_separation_threshold"] = _decimal_text(cfg.get("ema_separation_threshold"), "0.0015", 0.0, 0.05)
    cfg["ema_slope_threshold"] = _decimal_text(cfg.get("ema_slope_threshold"), "0.0002", 0.0, 0.01)
    cfg["ema_slope_lookback"] = _int(cfg.get("ema_slope_lookback"), 3, 1, 48)
    cfg["recent_cross_lookback"] = _int(cfg.get("recent_cross_lookback"), 24, 2, 200)
    cfg["max_recent_crosses"] = _int(cfg.get("max_recent_crosses"), 1, 0, 20)
    cfg["pullback_lookback"] = _int(cfg.get("pullback_lookback"), 4, 1, 20)
    cfg["pullback_tolerance_pct"] = _decimal_text(cfg.get("pullback_tolerance_pct"), "0.001", 0.0, 0.02)
    cfg["ema50_break_tolerance_pct"] = _decimal_text(cfg.get("ema50_break_tolerance_pct"), "0.001", 0.0, 0.02)
    cfg["atr_period"] = _int(cfg.get("atr_period"), 14, 2, 100)
    cfg["max_entry_candle_atr_multiple"] = _decimal_text(cfg.get("max_entry_candle_atr_multiple"), "1.8", 0.3, 10.0)
    cfg["max_entry_body_atr_multiple"] = _decimal_text(cfg.get("max_entry_body_atr_multiple"), "1.2", 0.2, 10.0)
    cfg["signal_max_age_seconds"] = _int(cfg.get("signal_max_age_seconds"), 180, 30, 900)

    cfg["estimated_taker_fee_rate"] = _decimal_text(cfg.get("estimated_taker_fee_rate"), "0.0005", 0.0, 0.005)
    cfg["estimated_maker_fee_rate"] = _decimal_text(cfg.get("estimated_maker_fee_rate"), "0.0002", 0.0, 0.005)
    cfg["slippage_buffer_pct"] = _decimal_text(cfg.get("slippage_buffer_pct"), "0.0005", 0.0, 0.01)
    cfg["max_margin_usage_pct"] = _decimal_text(cfg.get("max_margin_usage_pct"), "0.9", 0.1, 1.0)

    working_type = str(cfg.get("working_type") or "MARK_PRICE").strip().upper()
    cfg["working_type"] = working_type if working_type in {"MARK_PRICE", "CONTRACT_PRICE"} else "MARK_PRICE"
    tp_working_type = str(cfg.get("tp_working_type") or "CONTRACT_PRICE").strip().upper()
    cfg["tp_working_type"] = tp_working_type if tp_working_type in {"MARK_PRICE", "CONTRACT_PRICE"} else "CONTRACT_PRICE"
    cfg["protection_retry_attempts"] = _int(cfg.get("protection_retry_attempts"), 3, 1, 6)
    delays = cfg.get("protection_retry_delays_seconds")
    if not isinstance(delays, (list, tuple)) or not delays:
        delays = defaults["protection_retry_delays_seconds"]
    cfg["protection_retry_delays_seconds"] = [_float(v, 1.0, 0.0, 10.0) for v in list(delays)[:6]]
    cfg["protection_check_interval_seconds"] = _int(cfg.get("protection_check_interval_seconds"), 60, 10, 600)
    cfg["loop_interval_seconds"] = _int(cfg.get("loop_interval_seconds"), 10, 5, 60)
    cfg["rules_refresh_seconds"] = _int(cfg.get("rules_refresh_seconds"), 3600, 300, 86400)
    cfg["dry_run_balance_usdt"] = _decimal_text(cfg.get("dry_run_balance_usdt"), "210", 10.0, 10_000_000.0)

    # Prohibited behaviours stay off regardless of what is persisted.
    for key in ("allow_pyramiding", "allow_martingale", "allow_averaging"):
        cfg[key] = False
    cfg["allow_auto_raise_to_exchange_minimum"] = _bool(cfg.get("allow_auto_raise_to_exchange_minimum"), False)
    cfg.pop("allow_shared_account_with_main_bot", None)
    return cfg


def effective_trading_mode(cfg, environ=None) -> str:
    """LIVE only when config says LIVE and the environment does not force DRY_RUN."""
    environ = os.environ if environ is None else environ
    forced = str(environ.get(TRADING_MODE_ENV, "") or "").strip().upper()
    if forced == TRADING_MODE_DRY_RUN:
        return TRADING_MODE_DRY_RUN
    return TRADING_MODE_LIVE if (cfg or {}).get("trading_mode") == TRADING_MODE_LIVE else TRADING_MODE_DRY_RUN


def sizing_mode_for(cfg) -> str:
    key = "sizing_mode_testnet" if cfg.get("network") == NETWORK_TESTNET else "sizing_mode_mainnet"
    return cfg.get(key) or SIZING_FIXED


__all__ = (
    "CLIENT_ID_STRATEGY",
    "NETWORK_MAINNET",
    "NETWORK_TESTNET",
    "SIZING_FIXED",
    "SIZING_RISK_BASED",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "TRADING_MODE_DRY_RUN",
    "TRADING_MODE_ENV",
    "TRADING_MODE_LIVE",
    "default_btc_pullback_config",
    "effective_trading_mode",
    "normalize_btc_pullback_config",
    "sizing_mode_for",
)

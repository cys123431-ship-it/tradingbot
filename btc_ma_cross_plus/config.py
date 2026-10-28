"""Configuration for the /macross strategy (SMA3/SMA120 + selectable A~E features).

Feature flags (Telegram toggles, at least one required):
  A  trend filter      - SMA120 side + ADX / efficiency-ratio chop gate
  B  ATR initial stop  - structural stop from entry (no unprotected window)
  C  volatility sizing - size up in calm markets, down in wild ones
  D  chandelier trail  - ATR trailing stop instead of a fixed profit lock
  E  donchian breakout - trade the N-bar breakout as an extra trigger
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
    _int,
)

STRATEGY_NAME = "sma3_sma120_macross"
STRATEGY_VERSION = "1.0.0"
CLIENT_ID_STRATEGY = "mcross"
CONFIG_KEY = "ma_cross"

TIMEFRAMES = ("15m", "30m", "1h", "2h", "4h")
FEATURES = ("A", "B", "C", "D", "E")
FEATURE_LABELS = {
    "A": "추세필터(SMA120+ADX/ER)",
    "B": "ATR 초기손절",
    "C": "변동성 타겟 사이징",
    "D": "ATR 샹들리에 트레일링",
    "E": "Donchian 돌파 트리거",
}
DEFAULT_FEATURES = ("B",)
FEATURE_FIELDS = {
    "A": "feature_a_trend_filter",
    "B": "feature_b_atr_stop",
    "C": "feature_c_vol_sizing",
    "D": "feature_d_chandelier",
    "E": "feature_e_donchian",
}
SCAN_SCOPES = ("btc", "alts", "single")
ENTRY_MODES = ("cross", "trend")
EMERGENCY_STOP_CHOICES = (0, 10, 20, 30, 50)
LEVERAGE_CHOICES = (2, 3, 4, 5)
DEFAULT_LEVERAGE = 5
REENTRY_WAIT_CHOICES = (1, 3, 6, 0)


def default_ma_cross_config() -> dict:
    return {
        "enabled": False,
        "trading_mode": TRADING_MODE_DRY_RUN,
        "network": NETWORK_TESTNET,
        "symbol": "BTCUSDT",
        "strategy_name": STRATEGY_NAME,
        "timeframe": "4h",
        "scan_scope": "btc",
        "single_symbol": "BTCUSDT",
        "alt_universe_size": 50,
        "rank_testnet_by_mainnet_volume": True,
        "min_median_bar_range_pct": "0.0005",
        "max_zero_range_bar_share": "0.1",
        "entry_mode": "cross",
        "reentry_wait_bars": 3,
        "rotate_after_exit": False,
        "alt_exclude_btc": True,
        "fast_period": 3,
        "slow_period": 120,
        "candle_history": 500,
        "leverage": DEFAULT_LEVERAGE,
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "margin_fraction": "0.5",
        # C-only strategy: no emergency stop, exits are the opposite cross + profit lock.
        "emergency_stop_roi_percent": 0,
        # Feature A
        "feature_a_trend_filter": 0,
        "feature_a_adx_min": "18",
        "feature_a_efficiency_min": "0.20",
        # Feature B
        "feature_b_atr_stop": 0,
        "feature_b_atr_multiplier": "2.0",
        # Feature C (the only feature exposed in the Telegram UI)
        "feature_c_vol_sizing": 1,
        "feature_c_target_atr_pct": "0.03",
        "feature_c_min_scale": "0.5",
        "feature_c_max_scale": "1.5",
        # Feature D
        "feature_d_chandelier": 0,
        "feature_d_atr_multiplier": "3.0",
        # Feature E
        "feature_e_donchian": 0,
        "feature_e_period": 20,
        "locking": True,
        "lock_start_roi_percent": "5",
        "lock_step_percent": "5",
        "lock_gap_percent": "1",
        "working_type": "MARK_PRICE",
        "lock_working_type": "CONTRACT_PRICE",
        "tp_working_type": "CONTRACT_PRICE",
        "signal_max_age_seconds": 180,
        "reverse_retry_window_seconds": 600,
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


def feature_mode(cfg) -> str:
    """Canonical feature string such as ``"ABD"`` (order A..E, no duplicates)."""
    cfg = cfg or {}
    flags = []
    for name in FEATURES:
        if _bool(cfg.get(FEATURE_FIELDS[name]), False):
            flags.append(name)
    return "".join(flags)


def normalize_ma_cross_config(raw=None) -> dict:
    supplied = dict(raw) if isinstance(raw, dict) else {}
    cfg = deepcopy(default_ma_cross_config())
    cfg.update(supplied)
    defaults = default_ma_cross_config()
    cfg["enabled"] = _bool(cfg.get("enabled"), False)
    mode = str(cfg.get("trading_mode") or TRADING_MODE_DRY_RUN).strip().upper()
    cfg["trading_mode"] = mode if mode in TRADING_MODES else TRADING_MODE_DRY_RUN
    network = str(cfg.get("network") or NETWORK_TESTNET).strip().lower()
    cfg["network"] = network if network in NETWORKS else NETWORK_TESTNET
    cfg["symbol"] = "BTCUSDT"
    cfg["strategy_name"] = STRATEGY_NAME
    timeframe = str(cfg.get("timeframe") or "4h").strip().lower()
    cfg["timeframe"] = timeframe if timeframe in TIMEFRAMES else "4h"
    scope = str(cfg.get("scan_scope") or "btc").strip().lower()
    cfg["scan_scope"] = scope if scope in SCAN_SCOPES else "btc"
    single = "".join(ch for ch in str(cfg.get("single_symbol") or "").upper() if ch.isalnum())
    cfg["single_symbol"] = single if single.endswith("USDT") and len(single) > 4 else "BTCUSDT"
    cfg["alt_universe_size"] = _int(cfg.get("alt_universe_size"), 50, 5, 100)
    cfg["rank_testnet_by_mainnet_volume"] = _bool(cfg.get("rank_testnet_by_mainnet_volume"), True)
    cfg["min_median_bar_range_pct"] = _decimal_text(cfg.get("min_median_bar_range_pct"), "0.0005", 0.0, 0.01)
    cfg["max_zero_range_bar_share"] = _decimal_text(cfg.get("max_zero_range_bar_share"), "0.1", 0.0, 1.0)
    entry_mode = str(cfg.get("entry_mode") or "cross").strip().lower()
    cfg["entry_mode"] = entry_mode if entry_mode in ENTRY_MODES else "cross"
    try:
        wait = int(float(cfg.get("reentry_wait_bars")))
    except (TypeError, ValueError):
        wait = 3
    cfg["reentry_wait_bars"] = wait if wait in REENTRY_WAIT_CHOICES else 3
    cfg["rotate_after_exit"] = _bool(cfg.get("rotate_after_exit"), False)
    cfg["alt_exclude_btc"] = _bool(cfg.get("alt_exclude_btc"), True)
    cfg["fast_period"] = 3
    cfg["slow_period"] = 120
    cfg["candle_history"] = _int(cfg.get("candle_history"), 500, 210, 1500)
    try:
        leverage = int(float(cfg.get("leverage")))
    except (TypeError, ValueError):
        leverage = DEFAULT_LEVERAGE
    cfg["leverage"] = leverage if leverage in LEVERAGE_CHOICES else DEFAULT_LEVERAGE
    cfg["margin_type"] = "ISOLATED"
    cfg["position_mode"] = "ONE_WAY"
    cfg["margin_fraction"] = _decimal_text(cfg.get("margin_fraction"), "0.5", 0.05, 0.95)
    try:
        stop = int(float(cfg.get("emergency_stop_roi_percent")))
    except (TypeError, ValueError):
        stop = defaults["emergency_stop_roi_percent"]
    cfg["emergency_stop_roi_percent"] = stop if stop in EMERGENCY_STOP_CHOICES else defaults["emergency_stop_roi_percent"]
    for name, field in FEATURE_FIELDS.items():
        cfg[field] = 1 if _bool(cfg.get(field), False) else 0
    # This strategy runs C-only: normalisation pins C on and the other blocks off,
    # whatever an older config may still carry.
    cfg["feature_c_vol_sizing"] = 1
    for name in ("A", "B", "D", "E"):
        cfg[FEATURE_FIELDS[name]] = 0
    # Exit policy: opposite cross (+ profit lock).  No emergency stop, no
    # chandelier trail, no ATR initial stop.
    cfg["locking"] = True
    cfg["emergency_stop_roi_percent"] = 0
    cfg["feature_a_adx_min"] = _decimal_text(cfg.get("feature_a_adx_min"), "18", 0.0, 60.0)
    cfg["feature_a_efficiency_min"] = _decimal_text(cfg.get("feature_a_efficiency_min"), "0.20", 0.0, 1.0)
    cfg["feature_b_atr_multiplier"] = _decimal_text(cfg.get("feature_b_atr_multiplier"), "2.0", 0.5, 6.0)
    cfg["feature_c_target_atr_pct"] = _decimal_text(cfg.get("feature_c_target_atr_pct"), "0.03", 0.001, 0.5)
    cfg["feature_c_min_scale"] = _decimal_text(cfg.get("feature_c_min_scale"), "0.5", 0.1, 1.0)
    cfg["feature_c_max_scale"] = _decimal_text(cfg.get("feature_c_max_scale"), "1.5", 1.0, 3.0)
    cfg["feature_d_atr_multiplier"] = _decimal_text(cfg.get("feature_d_atr_multiplier"), "3.0", 1.0, 8.0)
    cfg["feature_e_period"] = _int(cfg.get("feature_e_period"), 20, 5, 100)
    return cfg


__all__ = (
    "CLIENT_ID_STRATEGY",
    "CONFIG_KEY",
    "DEFAULT_FEATURES",
    "DEFAULT_LEVERAGE",
    "EMERGENCY_STOP_CHOICES",
    "ENTRY_MODES",
    "FEATURES",
    "FEATURE_FIELDS",
    "FEATURE_LABELS",
    "LEVERAGE_CHOICES",
    "REENTRY_WAIT_CHOICES",
    "SCAN_SCOPES",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "TIMEFRAMES",
    "default_ma_cross_config",
    "feature_mode",
    "normalize_ma_cross_config",
)

"""Configuration contract for the isolated options sleeve."""

from __future__ import annotations

from copy import deepcopy


OPTIONS_CAPITAL_LIMIT_USDT = 20.0


def default_options_config() -> dict:
    return {
        "enabled": False,
        "capital_limit_usdt": OPTIONS_CAPITAL_LIMIT_USDT,
        "entry_fraction": 1.00,
        "scan_interval_seconds": 300,
        "manage_interval_seconds": 30,
        "underlyings": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
        "signal_timeframe": "1h",
        "slow_timeframe": "4h",
        "min_abs_signal": 0.46,
        "strong_signal": 0.68,
        "squeeze_min_score": 0.58,
        "squeeze_compression_ratio": 0.78,
        "squeeze_volume_multiplier": 1.15,
        "min_dte_days": 2.0,
        "max_dte_days": 21.0,
        "target_dte_days": 8.0,
        "target_otm_pct": 0.02,
        "min_abs_delta": 0.20,
        "max_abs_delta": 0.70,
        "preferred_delta_min": 0.35,
        "preferred_delta_max": 0.55,
        "max_spread_pct": 0.18,
        "max_iv_to_realized": 1.60,
        "hard_max_iv_to_realized": 2.40,
        "squeeze_max_iv_to_realized": 1.35,
        "min_quote_volume_usdt": 50.0,
        "take_profit_pct": 0.80,
        "stop_loss_pct": 0.45,
        "trail_activation_pct": 0.35,
        "trail_drawdown_pct": 0.25,
        "max_hold_hours": 72.0,
        "adaptive_time_stop_hours": 36.0,
        "expiry_exit_hours": 8.0,
        "near_expiry_risk_days": 2.0,
        "iv_collapse_exit_pct": 0.30,
        "theta_burden_exit_pct": 0.12,
        "delta_collapse_exit": 0.15,
        "exit_signal_refresh_seconds": 900,
        "max_candidates_per_underlying": 10,
        "request_timeout_seconds": 10,
        "rejection_stats_window": 100,
    }


def _float(value, default, low, high):
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    return min(float(high), max(float(low), result))


def _int(value, default, low, high):
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = int(default)
    return min(int(high), max(int(low), result))


def normalize_options_config(raw=None) -> dict:
    cfg = deepcopy(default_options_config())
    if isinstance(raw, dict):
        cfg.update(raw)

    cfg["enabled"] = bool(cfg.get("enabled", False))
    # This sleeve is deliberately fixed to the user's hard capital ceiling.
    cfg["capital_limit_usdt"] = OPTIONS_CAPITAL_LIMIT_USDT
    # Use the whole fixed sleeve when a contract fits. The fee is still included
    # inside the absolute 20 USDT ceiling by build_long_option_entry_plan().
    cfg["entry_fraction"] = 1.00
    cfg["scan_interval_seconds"] = _int(cfg.get("scan_interval_seconds"), 300, 60, 3600)
    cfg["manage_interval_seconds"] = _int(cfg.get("manage_interval_seconds"), 30, 15, 300)
    underlyings = cfg.get("underlyings")
    if not isinstance(underlyings, (list, tuple)):
        underlyings = default_options_config()["underlyings"]
    normalized_underlyings = []
    for value in underlyings:
        symbol = str(value or "").strip().upper().replace("/", "")
        if symbol and symbol.endswith("USDT") and symbol not in normalized_underlyings:
            normalized_underlyings.append(symbol)
    cfg["underlyings"] = normalized_underlyings[:8] or ["BTCUSDT", "ETHUSDT"]

    cfg["signal_timeframe"] = "1h"
    cfg["slow_timeframe"] = "4h"
    cfg["min_abs_signal"] = _float(cfg.get("min_abs_signal"), 0.46, 0.30, 0.90)
    cfg["strong_signal"] = _float(cfg.get("strong_signal"), 0.68, 0.50, 0.98)
    if cfg["strong_signal"] <= cfg["min_abs_signal"]:
        cfg["strong_signal"] = min(0.98, cfg["min_abs_signal"] + 0.15)
    cfg["squeeze_min_score"] = _float(cfg.get("squeeze_min_score"), 0.58, 0.35, 0.95)
    cfg["squeeze_compression_ratio"] = _float(cfg.get("squeeze_compression_ratio"), 0.78, 0.45, 0.95)
    cfg["squeeze_volume_multiplier"] = _float(cfg.get("squeeze_volume_multiplier"), 1.15, 1.0, 3.0)
    cfg["min_dte_days"] = _float(cfg.get("min_dte_days"), 2.0, 1.0, 30.0)
    cfg["max_dte_days"] = _float(cfg.get("max_dte_days"), 21.0, 2.0, 90.0)
    if cfg["max_dte_days"] <= cfg["min_dte_days"]:
        cfg["max_dte_days"] = cfg["min_dte_days"] + 1.0
    cfg["target_dte_days"] = _float(cfg.get("target_dte_days"), 8.0, cfg["min_dte_days"], cfg["max_dte_days"])
    cfg["target_otm_pct"] = _float(cfg.get("target_otm_pct"), 0.02, 0.0, 0.15)
    cfg["min_abs_delta"] = _float(cfg.get("min_abs_delta"), 0.20, 0.10, 0.60)
    cfg["max_abs_delta"] = _float(cfg.get("max_abs_delta"), 0.70, 0.30, 0.95)
    if cfg["max_abs_delta"] <= cfg["min_abs_delta"]:
        cfg["max_abs_delta"] = min(0.95, cfg["min_abs_delta"] + 0.20)
    cfg["preferred_delta_min"] = _float(cfg.get("preferred_delta_min"), 0.35, cfg["min_abs_delta"], cfg["max_abs_delta"])
    cfg["preferred_delta_max"] = _float(cfg.get("preferred_delta_max"), 0.55, cfg["min_abs_delta"], cfg["max_abs_delta"])
    if cfg["preferred_delta_max"] <= cfg["preferred_delta_min"]:
        cfg["preferred_delta_max"] = min(cfg["max_abs_delta"], cfg["preferred_delta_min"] + 0.15)
    cfg["max_spread_pct"] = _float(cfg.get("max_spread_pct"), 0.18, 0.02, 0.40)
    cfg["max_iv_to_realized"] = _float(cfg.get("max_iv_to_realized"), 1.60, 0.70, 3.00)
    cfg["hard_max_iv_to_realized"] = _float(cfg.get("hard_max_iv_to_realized"), 2.40, cfg["max_iv_to_realized"], 5.00)
    cfg["squeeze_max_iv_to_realized"] = _float(cfg.get("squeeze_max_iv_to_realized"), 1.35, 0.60, cfg["hard_max_iv_to_realized"])
    cfg["min_quote_volume_usdt"] = _float(cfg.get("min_quote_volume_usdt"), 50.0, 0.0, 100000.0)
    cfg["take_profit_pct"] = _float(cfg.get("take_profit_pct"), 0.80, 0.20, 5.00)
    cfg["stop_loss_pct"] = _float(cfg.get("stop_loss_pct"), 0.45, 0.10, 0.90)
    cfg["trail_activation_pct"] = _float(cfg.get("trail_activation_pct"), 0.35, 0.10, 3.00)
    cfg["trail_drawdown_pct"] = _float(cfg.get("trail_drawdown_pct"), 0.25, 0.05, 0.75)
    cfg["max_hold_hours"] = _float(cfg.get("max_hold_hours"), 72.0, 2.0, 720.0)
    cfg["adaptive_time_stop_hours"] = _float(cfg.get("adaptive_time_stop_hours"), 36.0, 4.0, cfg["max_hold_hours"])
    cfg["expiry_exit_hours"] = _float(cfg.get("expiry_exit_hours"), 8.0, 1.0, 48.0)
    cfg["near_expiry_risk_days"] = _float(cfg.get("near_expiry_risk_days"), 2.0, 0.5, 7.0)
    cfg["iv_collapse_exit_pct"] = _float(cfg.get("iv_collapse_exit_pct"), 0.30, 0.10, 0.80)
    cfg["theta_burden_exit_pct"] = _float(cfg.get("theta_burden_exit_pct"), 0.12, 0.03, 0.50)
    cfg["delta_collapse_exit"] = _float(cfg.get("delta_collapse_exit"), 0.15, 0.05, cfg["min_abs_delta"])
    cfg["exit_signal_refresh_seconds"] = _int(cfg.get("exit_signal_refresh_seconds"), 900, 300, 3600)
    cfg["max_candidates_per_underlying"] = _int(cfg.get("max_candidates_per_underlying"), 10, 1, 20)
    cfg["request_timeout_seconds"] = _int(cfg.get("request_timeout_seconds"), 10, 3, 30)
    cfg["rejection_stats_window"] = _int(cfg.get("rejection_stats_window"), 100, 20, 500)
    return cfg


__all__ = (
    "OPTIONS_CAPITAL_LIMIT_USDT",
    "default_options_config",
    "normalize_options_config",
)

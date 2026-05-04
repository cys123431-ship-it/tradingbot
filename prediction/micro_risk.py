"""10 USDT capped micro risk controls for prediction markets."""

from __future__ import annotations


DEFAULT_PREDICTION_MICRO_CONFIG = {
    "enabled": False,
    "paper_only": True,
    "equity_cap_usdt": 10.0,
    "min_stake_usdt": 1.0,
    "max_stake_usdt": 1.0,
    "daily_loss_limit_usdt": 1.0,
    "max_daily_trades": 2,
    "max_open_positions": 1,
    "min_edge_probability": 0.03,
    "max_fee_burden_pct": 30.0,
    "allowed_market_types": ["crypto", "macro"],
    "use_mainnet": False,
    "scan_limit": 30,
    "scan_interval_seconds": 300,
    "auto_paper_entry": True,
}


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _finite_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def default_prediction_micro_config():
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_PREDICTION_MICRO_CONFIG.items()
    }


def normalize_prediction_micro_config(raw=None):
    cfg = default_prediction_micro_config()
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["enabled"] = str(cfg.get("enabled")).lower() in {"1", "true", "yes", "on", "enabled"}
    cfg["paper_only"] = True
    for key in ("equity_cap_usdt", "min_stake_usdt", "max_stake_usdt", "daily_loss_limit_usdt", "min_edge_probability", "max_fee_burden_pct"):
        cfg[key] = max(0.0, _finite_float(cfg.get(key), DEFAULT_PREDICTION_MICRO_CONFIG[key]))
    for key in ("max_daily_trades", "max_open_positions"):
        cfg[key] = max(0, _finite_int(cfg.get(key), DEFAULT_PREDICTION_MICRO_CONFIG[key]))
    for key in ("scan_limit", "scan_interval_seconds"):
        cfg[key] = max(1, _finite_int(cfg.get(key), DEFAULT_PREDICTION_MICRO_CONFIG[key]))
    cfg["use_mainnet"] = str(cfg.get("use_mainnet")).lower() in {"1", "true", "yes", "on", "enabled"}
    cfg["auto_paper_entry"] = str(cfg.get("auto_paper_entry")).lower() in {"1", "true", "yes", "on", "enabled"}
    if cfg["equity_cap_usdt"] <= 0 or cfg["equity_cap_usdt"] > 10.0:
        cfg["equity_cap_usdt"] = 10.0
    if cfg["max_stake_usdt"] < cfg["min_stake_usdt"]:
        cfg["max_stake_usdt"] = cfg["min_stake_usdt"]
    return cfg


def build_prediction_micro_plan(
    *,
    market,
    side,
    market_price,
    edge,
    total_allocated_usdt=0.0,
    daily_realized_pnl_usdt=0.0,
    daily_trade_count=0,
    open_position_count=0,
    requested_stake_usdt=None,
    cfg=None,
):
    cfg = normalize_prediction_micro_config(cfg)
    market = market or {}
    stake = _finite_float(requested_stake_usdt, cfg["max_stake_usdt"])
    stake = min(max(stake, cfg["min_stake_usdt"]), cfg["max_stake_usdt"])
    allocated = max(0.0, _finite_float(total_allocated_usdt))
    daily_pnl = _finite_float(daily_realized_pnl_usdt)
    price = _finite_float(market_price)
    edge = _finite_float(edge)
    reject = None
    if market.get("market_type") not in set(cfg.get("allowed_market_types") or []):
        reject = "REJECTED_PREDICTION_CATEGORY"
    elif daily_pnl <= -cfg["daily_loss_limit_usdt"]:
        reject = "REJECTED_PREDICTION_DAILY_LOSS"
    elif int(daily_trade_count or 0) >= cfg["max_daily_trades"]:
        reject = "REJECTED_PREDICTION_DAILY_TRADES"
    elif int(open_position_count or 0) >= cfg["max_open_positions"]:
        reject = "REJECTED_PREDICTION_OPEN_POSITION_LIMIT"
    elif stake < cfg["min_stake_usdt"]:
        reject = "REJECTED_PREDICTION_STAKE_LOW"
    elif allocated + stake > cfg["equity_cap_usdt"] + 1e-12:
        reject = "REJECTED_PREDICTION_EQUITY_CAP"
    elif price <= 0.0 or price >= 1.0:
        reject = "REJECTED_PREDICTION_PRICE"
    elif edge < cfg["min_edge_probability"]:
        reject = "REJECTED_PREDICTION_EDGE_LOW"

    if reject:
        return {
            "accepted": False,
            "reject_code": reject,
            "paper_only": True,
            "equity_cap_usdt": cfg["equity_cap_usdt"],
            "stake_usdt": stake,
        }
    shares = stake / price
    return {
        "accepted": True,
        "paper_only": True,
        "market_id": market.get("id"),
        "market_title": market.get("title"),
        "market_type": market.get("market_type"),
        "side": str(side or "YES").upper(),
        "stake_usdt": stake,
        "entry_price": price,
        "shares": shares,
        "max_loss_usdt": stake,
        "max_payout_usdt": shares,
        "max_profit_usdt": shares - stake,
        "edge_probability": edge,
        "equity_cap_usdt": cfg["equity_cap_usdt"],
        "paper_only": True,
    }

"""Pure helpers for the independent EMA200 + UT Bot + RSI 2H strategy."""

from __future__ import annotations

from math import isfinite

EMA200_UTBOT_RSI_STRATEGY = "ema200_utbot_rsi_2h"
EMA200_UTBOT_RSI_CONFIG_KEY = "EMA200UTBotRSI2H"
EMA200_UTBOT_RSI_DISPLAY_NAME = "EMA200 + UT Bot + RSI (2H)"


def default_ema200_utbot_rsi_config():
    return {
        "enabled": True,
        "timeframe": "2h",
        "ema_period": 200,
        "rsi_length": 14,
        "rsi_threshold": 50.0,
        "risk_per_trade_percent": 0.50,
        "min_risk_per_trade_percent": 0.10,
        "max_risk_per_trade_percent": 5.00,
        "leverage": 5,
        "min_leverage": 1,
        "max_leverage": 10,
        "emergency_exit_percent": 5.0,
        "min_emergency_exit_percent": 0.5,
        "max_emergency_exit_percent": 30.0,
        "daily_loss_limit_percent": 2.0,
        "min_daily_loss_limit_percent": 0.5,
        "max_daily_loss_limit_percent": 20.0,
        "weekly_loss_limit_percent": 5.0,
        "min_weekly_loss_limit_percent": 1.0,
        "max_weekly_loss_limit_percent": 40.0,
    }


def _finite(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if isfinite(parsed) else float(default)


def _bounded(value, default, low, high):
    return max(float(low), min(float(high), _finite(value, default)))


def _enabled_value(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    return True


def normalize_ema200_utbot_rsi_config(raw=None):
    defaults = default_ema200_utbot_rsi_config()
    cfg = dict(defaults)
    if isinstance(raw, dict):
        cfg.update(raw)

    cfg["enabled"] = _enabled_value(cfg.get("enabled", True))
    # The strategy definition is intentionally fixed to 2h / EMA200 / RSI50.
    cfg["timeframe"] = "2h"
    cfg["ema_period"] = 200
    cfg["rsi_threshold"] = 50.0
    try:
        cfg["rsi_length"] = max(2, min(100, int(cfg.get("rsi_length", 14) or 14)))
    except (TypeError, ValueError, OverflowError):
        cfg["rsi_length"] = 14

    # These are product safety boundaries, not user-tunable settings.  Keeping
    # them immutable also prevents an older or hand-edited config from silently
    # widening the Telegram validation range.
    min_risk = float(defaults["min_risk_per_trade_percent"])
    max_risk = float(defaults["max_risk_per_trade_percent"])
    cfg["min_risk_per_trade_percent"] = min_risk
    cfg["max_risk_per_trade_percent"] = max_risk
    cfg["risk_per_trade_percent"] = _bounded(
        cfg.get("risk_per_trade_percent"),
        defaults["risk_per_trade_percent"],
        min_risk,
        max_risk,
    )

    min_leverage = int(defaults["min_leverage"])
    max_leverage = int(defaults["max_leverage"])
    try:
        leverage = int(cfg.get("leverage", defaults["leverage"]) or defaults["leverage"])
    except (TypeError, ValueError, OverflowError):
        leverage = defaults["leverage"]
    cfg["min_leverage"] = min_leverage
    cfg["max_leverage"] = max_leverage
    cfg["leverage"] = max(min_leverage, min(max_leverage, leverage))

    min_emergency = float(defaults["min_emergency_exit_percent"])
    max_emergency = float(defaults["max_emergency_exit_percent"])
    cfg["min_emergency_exit_percent"] = min_emergency
    cfg["max_emergency_exit_percent"] = max_emergency
    cfg["emergency_exit_percent"] = _bounded(
        cfg.get("emergency_exit_percent"),
        defaults["emergency_exit_percent"],
        min_emergency,
        max_emergency,
    )

    min_daily = float(defaults["min_daily_loss_limit_percent"])
    max_daily = float(defaults["max_daily_loss_limit_percent"])
    daily = _bounded(
        cfg.get("daily_loss_limit_percent"),
        defaults["daily_loss_limit_percent"],
        min_daily,
        max_daily,
    )
    cfg["min_daily_loss_limit_percent"] = min_daily
    cfg["max_daily_loss_limit_percent"] = max_daily
    cfg["daily_loss_limit_percent"] = daily

    min_weekly = float(defaults["min_weekly_loss_limit_percent"])
    max_weekly = float(defaults["max_weekly_loss_limit_percent"])
    weekly = _bounded(
        cfg.get("weekly_loss_limit_percent"),
        defaults["weekly_loss_limit_percent"],
        min_weekly,
        max_weekly,
    )
    cfg["min_weekly_loss_limit_percent"] = min_weekly
    cfg["max_weekly_loss_limit_percent"] = max_weekly
    cfg["weekly_loss_limit_percent"] = max(daily, weekly)
    return cfg


def evaluate_ema200_utbot_rsi_entry(
    *,
    close_price,
    ema200,
    ut_state,
    ut_last_signal_side,
    ut_last_signal_ts,
    prev_rsi,
    curr_rsi,
    rsi_signal_ts,
    threshold=50.0,
):
    """Evaluate the exact ordered entry rule.

    A previously-seen RSI cross is never remembered. The RSI cross must occur on
    the current completed candle, while the latest UT signal is already the same
    direction and its state is still active.
    """
    close_price = _finite(close_price, 0.0)
    ema200 = _finite(ema200, 0.0)
    prev_rsi = _finite(prev_rsi, 50.0)
    curr_rsi = _finite(curr_rsi, 50.0)
    threshold = _finite(threshold, 50.0)
    ut_state = str(ut_state or "").lower()
    ut_last_signal_side = str(ut_last_signal_side or "").lower()
    try:
        ut_ts = int(ut_last_signal_ts or 0)
    except (TypeError, ValueError):
        ut_ts = 0
    try:
        rsi_ts = int(rsi_signal_ts or 0)
    except (TypeError, ValueError):
        rsi_ts = 0

    rsi_cross_up = prev_rsi < threshold and curr_rsi > threshold
    rsi_cross_down = prev_rsi > threshold and curr_rsi < threshold
    # A UT state must already exist before the RSI-cross candle.  Signals on the
    # same completed candle are deliberately rejected because their intrabar
    # order cannot be proven from OHLCV data.
    ut_precedes_rsi = bool(ut_ts and rsi_ts and ut_ts < rsi_ts)

    detail = {
        "close": close_price,
        "ema200": ema200,
        "ema_long_ok": close_price > ema200,
        "ema_short_ok": close_price < ema200,
        "ut_state": ut_state or None,
        "ut_last_signal_side": ut_last_signal_side or None,
        "ut_last_signal_ts": ut_ts or None,
        "rsi_signal_ts": rsi_ts or None,
        "prev_rsi": prev_rsi,
        "curr_rsi": curr_rsi,
        "rsi_cross_up": rsi_cross_up,
        "rsi_cross_down": rsi_cross_down,
        "ut_precedes_rsi": ut_precedes_rsi,
    }

    long_ok = (
        close_price > ema200
        and ut_state == "long"
        and ut_last_signal_side == "long"
        and ut_precedes_rsi
        and rsi_cross_up
    )
    short_ok = (
        close_price < ema200
        and ut_state == "short"
        and ut_last_signal_side == "short"
        and ut_precedes_rsi
        and rsi_cross_down
    )

    if long_ok:
        return "long", "EMA200 위 + UT Buy 상태 유지 중 RSI 50 상향돌파", detail
    if short_ok:
        return "short", "EMA200 아래 + UT Sell 상태 유지 중 RSI 50 하향돌파", detail

    reasons = []
    if close_price > ema200:
        reasons.append("EMA200 위")
    elif close_price < ema200:
        reasons.append("EMA200 아래")
    else:
        reasons.append("EMA200 동일")
    reasons.append(f"UT {ut_state.upper() if ut_state else 'NONE'}")
    if not ut_precedes_rsi:
        reasons.append("UT 신호가 RSI 돌파보다 먼저 확정되지 않음")
    elif rsi_cross_up:
        reasons.append("RSI50 상향돌파")
    elif rsi_cross_down:
        reasons.append("RSI50 하향돌파")
    else:
        reasons.append("RSI50 신규 돌파 없음")
    return None, " / ".join(reasons), detail


def build_ema200_utbot_rsi_risk_plan(
    *,
    account_equity,
    free_balance,
    entry_price,
    config=None,
    safety_buffer=0.98,
):
    cfg = normalize_ema200_utbot_rsi_config(config)
    equity = max(0.0, _finite(account_equity, 0.0))
    free = max(0.0, _finite(free_balance, 0.0))
    entry = _finite(entry_price, 0.0)
    if entry <= 0:
        raise ValueError("entry_price must be positive")
    if equity <= 0:
        equity = free
    if equity <= 0 or free <= 0:
        raise ValueError("account equity/free balance unavailable")

    risk_pct = float(cfg["risk_per_trade_percent"])
    emergency_pct = float(cfg["emergency_exit_percent"])
    leverage = int(cfg["leverage"])
    risk_budget = equity * risk_pct / 100.0
    stop_fraction = emergency_pct / 100.0
    if risk_budget <= 0 or stop_fraction <= 0:
        raise ValueError("risk budget or emergency distance unavailable")

    uncapped_notional = risk_budget / stop_fraction
    margin_cap_notional = free * leverage * max(0.0, min(1.0, _finite(safety_buffer, 0.98)))
    planned_notional = min(uncapped_notional, margin_cap_notional)
    qty = planned_notional / entry
    planned_margin = planned_notional / max(leverage, 1)
    planned_loss = planned_notional * stop_fraction

    return {
        "risk_per_trade_percent": risk_pct,
        "emergency_exit_percent": emergency_pct,
        "leverage": leverage,
        "risk_budget_usdt": risk_budget,
        "uncapped_notional": uncapped_notional,
        "planned_notional": planned_notional,
        "planned_margin": planned_margin,
        "planned_qty": qty,
        "planned_emergency_loss_usdt": planned_loss,
        "margin_cap_applied": planned_notional + 1e-12 < uncapped_notional,
    }


def calculate_ema200_utbot_rsi_emergency_stop_price(
    *,
    side,
    entry_price,
    config=None,
):
    """Return the strategy's immutable emergency-stop anchor for a fill."""
    cfg = normalize_ema200_utbot_rsi_config(config)
    entry = _finite(entry_price, 0.0)
    if entry <= 0:
        raise ValueError("entry_price must be positive")
    fraction = float(cfg["emergency_exit_percent"]) / 100.0
    side_key = str(side or "").strip().lower()
    if side_key == "long":
        return entry * (1.0 - fraction)
    if side_key == "short":
        return entry * (1.0 + fraction)
    raise ValueError("side must be long or short")


def evaluate_ema200_utbot_rsi_loss_gate(
    *,
    account_equity,
    daily_realized_pnl,
    weekly_realized_pnl,
    config=None,
):
    cfg = normalize_ema200_utbot_rsi_config(config)
    equity = max(0.0, _finite(account_equity, 0.0))
    daily_pnl = _finite(daily_realized_pnl, 0.0)
    weekly_pnl = _finite(weekly_realized_pnl, 0.0)
    daily_limit = equity * float(cfg["daily_loss_limit_percent"]) / 100.0
    weekly_limit = equity * float(cfg["weekly_loss_limit_percent"]) / 100.0
    daily_blocked = equity > 0 and daily_pnl <= -daily_limit
    weekly_blocked = equity > 0 and weekly_pnl <= -weekly_limit
    reasons = []
    if daily_blocked:
        reasons.append(
            f"오늘 실현손익 {daily_pnl:.4f} USDT가 일일 손실한도 -{daily_limit:.4f} USDT 이하"
        )
    if weekly_blocked:
        reasons.append(
            f"최근 7일 실현손익 {weekly_pnl:.4f} USDT가 주간 손실한도 -{weekly_limit:.4f} USDT 이하"
        )
    return {
        "allowed": not (daily_blocked or weekly_blocked),
        "daily_blocked": daily_blocked,
        "weekly_blocked": weekly_blocked,
        "daily_limit_usdt": daily_limit,
        "weekly_limit_usdt": weekly_limit,
        "daily_realized_pnl": daily_pnl,
        "weekly_realized_pnl": weekly_pnl,
        "reason": " / ".join(reasons) if reasons else "신규 진입 허용",
    }


__all__ = (
    "EMA200_UTBOT_RSI_STRATEGY",
    "EMA200_UTBOT_RSI_CONFIG_KEY",
    "EMA200_UTBOT_RSI_DISPLAY_NAME",
    "default_ema200_utbot_rsi_config",
    "normalize_ema200_utbot_rsi_config",
    "evaluate_ema200_utbot_rsi_entry",
    "build_ema200_utbot_rsi_risk_plan",
    "calculate_ema200_utbot_rsi_emergency_stop_price",
    "evaluate_ema200_utbot_rsi_loss_gate",
)

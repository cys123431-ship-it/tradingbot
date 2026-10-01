"""Pure helpers for the independent EMA200 + UT Bot + RSI 2H strategy."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from zoneinfo import ZoneInfo

EMA200_UTBOT_RSI_STRATEGY = "ema200_utbot_rsi_2h"
EMA200_UTBOT_RSI_CONFIG_KEY = "EMA200UTBotRSI2H"
EMA200_UTBOT_RSI_DISPLAY_NAME = "EMA200 + UT Bot + RSI"

# This strategy owns its UT Bot definition.  It must never inherit the mutable
# settings used by the standalone /utbot strategy: doing so can turn the same
# completed candle from SHORT into LONG after an unrelated menu change.
EMA200_UTBOT_KEY_VALUE = 1.0
EMA200_UTBOT_ATR_PERIOD = 10
EMA200_UTBOT_USE_HEIKIN_ASHI = False
EMA200_DAILY_LOSS_RESET_STATE_KEY = "ema200_utbot_rsi_daily_loss_reset"
EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY = (
    "ema200_utbot_rsi_consecutive_loss_reset"
)
EMA200_KST = ZoneInfo("Asia/Seoul")

EMA200_SMALL_ACCOUNT_THRESHOLD_USDT = 1000.0
EMA200_SMALL_ACCOUNT_LEVERAGE = 5
EMA200_SMALL_ACCOUNT_MARGIN_LADDER_PERCENT = (50.0, 35.0, 25.0, 15.0, 10.0)
EMA200_EXIT_TIMEFRAMES = ("15m", "30m", "1h")
# Completed-candle entry timeframes selectable from Telegram; 2h is default.
EMA200_ENTRY_TIMEFRAMES = ("1h", "2h", "4h", "6h", "8h", "12h", "1d")
# "entry" exits on the UT Bot of the entry timeframe itself (the default since
# strategy revision 2); 15m/30m/1h keep the legacy faster exits selectable.
EMA200_EXIT_ON_ENTRY_TIMEFRAME = "entry"
EMA200_EXIT_MODES = (EMA200_EXIT_ON_ENTRY_TIMEFRAME,) + EMA200_EXIT_TIMEFRAMES
EMA200_STRATEGY_REVISION = 2
EMA200_TIMEFRAME_MS = {
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
EMA200_DEFAULT_ENTRY_TIMEFRAME = "2h"

EMA200_MIN_QUOTE_VOLUME_USDT = 200_000_000.0


def select_ema200_volume_symbols(tickers, markets):
    """Active linear USDT perpetuals with at least 200M USDT 24h turnover."""
    if not isinstance(tickers, dict) or not isinstance(markets, dict):
        return ()
    eligible = []
    for symbol, ticker in tickers.items():
        market = markets.get(symbol)
        if not isinstance(market, dict) or not isinstance(ticker, dict):
            continue
        if not (market.get('active') is True and market.get('swap') is True
                and market.get('linear') is True and market.get('quote') == 'USDT'
                and market.get('settle') == 'USDT'):
            continue
        info = ticker.get('info') if isinstance(ticker.get('info'), dict) else {}
        try:
            raw_volume = ticker.get('quoteVolume')
            if raw_volume is None:
                raw_volume = info.get('quoteVolume')
            quote_volume = float(raw_volume)
        except (TypeError, ValueError, OverflowError):
            continue
        if isfinite(quote_volume) and quote_volume >= EMA200_MIN_QUOTE_VOLUME_USDT:
            eligible.append((symbol, quote_volume))
    eligible.sort(key=lambda item: (-item[1], item[0]))
    return tuple(symbol for symbol, _ in eligible)


def is_ema200_utbot_rsi_symbol_allowed(symbol, eligible_symbols):
    """Accept only a symbol present in the current validated volume universe."""
    return bool(symbol and symbol in (eligible_symbols or ()))


def default_ema200_utbot_rsi_config():
    return {
        "enabled": True,
        "timeframe": "2h",
        "exit_timeframe": EMA200_EXIT_ON_ENTRY_TIMEFRAME,
        "ema_period": 200,
        "rsi_length": 14,
        "rsi_threshold": 50.0,
        "utbot_key_value": EMA200_UTBOT_KEY_VALUE,
        "utbot_atr_period": EMA200_UTBOT_ATR_PERIOD,
        "utbot_use_heikin_ashi": EMA200_UTBOT_USE_HEIKIN_ASHI,
        # When enabled, all valid signals from the current volume universe
        # are ranked on the same completed 2h candle before one entry is sent.
        # It remains independently switchable from the strategy entry toggle.
        "best_candidate_selection_enabled": True,
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
        # The sub-$1,000 plan is deliberately fixed rather than exposed as a
        # free-form Telegram setting.  It starts aggressively, then reduces
        # the next position after each consecutive losing EMA200 trade.
        "small_account_threshold_usdt": EMA200_SMALL_ACCOUNT_THRESHOLD_USDT,
        "small_account_leverage": EMA200_SMALL_ACCOUNT_LEVERAGE,
        "small_account_margin_ladder_percent": list(
            EMA200_SMALL_ACCOUNT_MARGIN_LADDER_PERCENT
        ),
        # Revision 2 small-account sizing: every entry has an exchange stop
        # at an ATR distance (capped by emergency_exit_percent) and its size is
        # chosen so that stop loses this share of equity.  The margin ladder
        # remains an upper cap that still shrinks after consecutive losses.
        "small_account_risk_sizing_enabled": True,
        "small_account_risk_percent": 1.5,
        "min_small_account_risk_percent": 0.25,
        "max_small_account_risk_percent": 3.0,
        "atr_stop_period": 14,
        "atr_stop_multiplier": 2.0,
        "atr_stop_min_percent": 1.0,
        # Profit stops start only above this margin ROI and lock one step
        # (5%p) below the achieved step, so a winner keeps room to run.
        "profit_stop_start_roi_percent": 15.0,
        "profit_stop_step_percent": 5.0,
        # Completed entry-timeframe candles to wait after an EMA200 exit.
        "reentry_cooldown_candles": 1,
    }


def ema200_effective_exit_timeframe(cfg):
    """Resolve the exit timeframe ("entry" follows the entry timeframe)."""
    exit_tf = str((cfg or {}).get("exit_timeframe") or "").strip().lower()
    if exit_tf == EMA200_EXIT_ON_ENTRY_TIMEFRAME or exit_tf not in EMA200_EXIT_MODES:
        return str((cfg or {}).get("timeframe") or EMA200_DEFAULT_ENTRY_TIMEFRAME)
    return exit_tf


def ema200_exit_timeframe_label(cfg):
    exit_tf = ema200_effective_exit_timeframe(cfg)
    if str((cfg or {}).get("exit_timeframe") or "") == EMA200_EXIT_ON_ENTRY_TIMEFRAME:
        return f"{exit_tf}(진입봉)"
    return exit_tf


def ema200_atr_stop_percent(rows, *, entry_price, period=14, multiplier=2.0,
                            min_percent=1.0, max_percent=5.0):
    """Return the ATR stop distance in % of the entry price, or None.

    Uses Wilder ATR on completed candles only (the last row is forming) and
    clamps the result to [min_percent, max_percent].
    """
    entry = _finite(entry_price, 0.0)
    closed = [row for row in (rows or [])[:-1] if row and len(row) >= 5]
    period = max(2, int(period or 14))
    if entry <= 0 or len(closed) < period + 1:
        return None
    true_ranges = []
    for index in range(1, len(closed)):
        high = _finite(closed[index][2], 0.0)
        low = _finite(closed[index][3], 0.0)
        prev_close = _finite(closed[index - 1][4], 0.0)
        if high <= 0 or low <= 0 or prev_close <= 0:
            return None
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    if not isfinite(atr) or atr <= 0:
        return None
    percent = atr * float(multiplier) / entry * 100.0
    low_bound = float(min_percent)
    high_bound = max(low_bound, float(max_percent))
    return max(low_bound, min(high_bound, percent))


def ema200_reentry_allowed_at_ms(exit_time, entry_timeframe, candles=1):
    """First moment a new entry is allowed after an exit, in epoch ms."""
    if not exit_time or int(candles or 0) <= 0:
        return None
    try:
        exited = datetime.fromisoformat(str(exit_time).replace("Z", "+00:00"))
    except ValueError:
        return None
    if exited.tzinfo is None:
        exited = exited.replace(tzinfo=timezone.utc)
    tf_ms = EMA200_TIMEFRAME_MS.get(str(entry_timeframe), 7_200_000)
    exit_ms = int(exited.timestamp() * 1000)
    return (exit_ms // tf_ms) * tf_ms + int(candles) * tf_ms


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
    # EMA200 / RSI50 and the strategy's own UT Bot values stay fixed; only the
    # completed-candle entry timeframe is operator-selectable.  In particular,
    # do not let the standalone UTBot menu mutate this strategy's signals.
    entry_tf = str(cfg.get("timeframe") or "").strip().lower()
    cfg["timeframe"] = (
        entry_tf
        if entry_tf in EMA200_ENTRY_TIMEFRAMES
        else EMA200_DEFAULT_ENTRY_TIMEFRAME
    )
    exit_tf = str(cfg.get("exit_timeframe") or "").strip().lower()
    cfg["exit_timeframe"] = (
        exit_tf if exit_tf in EMA200_EXIT_MODES else EMA200_EXIT_ON_ENTRY_TIMEFRAME
    )
    cfg["small_account_risk_sizing_enabled"] = _enabled_value(
        cfg.get("small_account_risk_sizing_enabled", True)
    )
    cfg["min_small_account_risk_percent"] = float(defaults["min_small_account_risk_percent"])
    cfg["max_small_account_risk_percent"] = float(defaults["max_small_account_risk_percent"])
    cfg["small_account_risk_percent"] = _bounded(
        cfg.get("small_account_risk_percent"),
        defaults["small_account_risk_percent"],
        cfg["min_small_account_risk_percent"],
        cfg["max_small_account_risk_percent"],
    )
    try:
        cfg["atr_stop_period"] = max(5, min(50, int(cfg.get("atr_stop_period") or 14)))
    except (TypeError, ValueError, OverflowError):
        cfg["atr_stop_period"] = 14
    cfg["atr_stop_multiplier"] = _bounded(cfg.get("atr_stop_multiplier"), 2.0, 1.0, 5.0)
    cfg["atr_stop_min_percent"] = _bounded(cfg.get("atr_stop_min_percent"), 1.0, 0.3, 5.0)
    cfg["profit_stop_start_roi_percent"] = _bounded(
        cfg.get("profit_stop_start_roi_percent"), 15.0, 5.0, 50.0
    )
    cfg["profit_stop_step_percent"] = _bounded(
        cfg.get("profit_stop_step_percent"), 5.0, 1.0, 20.0
    )
    try:
        cfg["reentry_cooldown_candles"] = max(
            0, min(6, int(cfg.get("reentry_cooldown_candles", 1)))
        )
    except (TypeError, ValueError, OverflowError):
        cfg["reentry_cooldown_candles"] = 1
    cfg["ema_period"] = 200
    cfg["rsi_threshold"] = 50.0
    cfg["utbot_key_value"] = EMA200_UTBOT_KEY_VALUE
    cfg["utbot_atr_period"] = EMA200_UTBOT_ATR_PERIOD
    cfg["utbot_use_heikin_ashi"] = EMA200_UTBOT_USE_HEIKIN_ASHI
    cfg["best_candidate_selection_enabled"] = _enabled_value(
        cfg.get("best_candidate_selection_enabled", True)
    )
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

    # Product-defined values: old or hand-edited config must not silently
    # change the live small-account contract.
    cfg["small_account_threshold_usdt"] = EMA200_SMALL_ACCOUNT_THRESHOLD_USDT
    cfg["small_account_leverage"] = EMA200_SMALL_ACCOUNT_LEVERAGE
    cfg["small_account_margin_ladder_percent"] = list(
        EMA200_SMALL_ACCOUNT_MARGIN_LADDER_PERCENT
    )
    return cfg


def count_ema200_consecutive_losses(recent_pnls):
    """Count newest-first consecutive EMA200 losses.

    A profitable or break-even close resets the sequence. Invalid persisted
    values are rejected instead of being interpreted as a safe zero-loss
    history, because streak zero intentionally permits the no-stop first stage.
    """
    count = 0
    for raw_pnl in recent_pnls or []:
        try:
            pnl = float(raw_pnl)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid realized PnL in EMA200 trade history") from exc
        if not isfinite(pnl):
            raise ValueError("non-finite realized PnL in EMA200 trade history")
        if pnl < 0:
            count += 1
            continue
        break
    return count


def ema200_small_account_margin_percent(consecutive_losses):
    """Return the fixed margin step for a non-negative loss streak."""
    try:
        streak = int(consecutive_losses or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("consecutive_losses must be a non-negative integer") from exc
    if streak < 0:
        raise ValueError("consecutive_losses must be a non-negative integer")
    ladder = EMA200_SMALL_ACCOUNT_MARGIN_LADDER_PERCENT
    return float(ladder[min(streak, len(ladder) - 1)])


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
    """Evaluate the ordered UT-first entry rule on one completed candle.

    The latest UT signal must predate the current candle and its bias must still
    be active.  RSI is not latched: LONG requires RSI above 50 and rising now;
    SHORT requires RSI below 50 and falling now.
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
    rsi_above_and_rising = curr_rsi > threshold and curr_rsi > prev_rsi
    rsi_below_and_falling = curr_rsi < threshold and curr_rsi < prev_rsi
    # A UT state must already exist before the current RSI candle. Signals on the
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
        "rsi_above_and_rising": rsi_above_and_rising,
        "rsi_below_and_falling": rsi_below_and_falling,
        "ut_precedes_rsi": ut_precedes_rsi,
    }

    long_ok = (
        close_price > ema200
        and ut_state == "long"
        and ut_last_signal_side == "long"
        and ut_precedes_rsi
        and rsi_above_and_rising
    )
    short_ok = (
        close_price < ema200
        and ut_state == "short"
        and ut_last_signal_side == "short"
        and ut_precedes_rsi
        and rsi_below_and_falling
    )

    if long_ok:
        return "long", "EMA200 위 + 선행 UT Buy 유지 + RSI 50 위에서 상승", detail
    if short_ok:
        return "short", "EMA200 아래 + 선행 UT Sell 유지 + RSI 50 아래에서 하락", detail

    reasons = []
    if close_price > ema200:
        reasons.append("EMA200 위")
    elif close_price < ema200:
        reasons.append("EMA200 아래")
    else:
        reasons.append("EMA200 동일")
    reasons.append(f"UT {ut_state.upper() if ut_state else 'NONE'}")
    if not ut_precedes_rsi:
        reasons.append("UT 신호가 현재 RSI 평가봉보다 먼저 확정되지 않음")
    elif rsi_above_and_rising:
        reasons.append("RSI50 위 상승")
    elif rsi_below_and_falling:
        reasons.append("RSI50 아래 하락")
    else:
        reasons.append("RSI 방향 조건 불충족")
    return None, " / ".join(reasons), detail


def build_ema200_utbot_rsi_risk_plan(
    *,
    account_equity,
    free_balance,
    entry_price,
    config=None,
    safety_buffer=0.98,
    consecutive_losses=0,
    stop_percent=None,
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

    emergency_pct = float(cfg["emergency_exit_percent"])
    margin_percent = ema200_small_account_margin_percent(consecutive_losses)
    loss_streak = int(consecutive_losses or 0)
    small_account = equity <= float(cfg["small_account_threshold_usdt"])
    if (
        small_account
        and stop_percent is not None
        and cfg["small_account_risk_sizing_enabled"]
    ):
        stop_pct = _finite(stop_percent, 0.0)
        if stop_pct <= 0:
            raise ValueError("ATR stop distance unavailable")
        leverage = int(cfg["small_account_leverage"])
        risk_pct = float(cfg["small_account_risk_percent"])
        risk_budget = equity * risk_pct / 100.0
        risk_notional = risk_budget / (stop_pct / 100.0)
        ladder_cap = equity * margin_percent / 100.0 * leverage
        free_cap = free * leverage * max(0.0, min(1.0, _finite(safety_buffer, 0.98)))
        planned_notional = min(risk_notional, ladder_cap, free_cap)
        planned_loss = planned_notional * stop_pct / 100.0
        return {
            "sizing_mode": "small_account_atr_risk",
            "small_account_mode": True,
            "small_account_threshold_usdt": float(cfg["small_account_threshold_usdt"]),
            "consecutive_losses": loss_streak,
            "margin_percent": margin_percent,
            "margin_cap_percent": margin_percent,
            "leverage": leverage,
            "emergency_exit_percent": stop_pct,
            "emergency_stop_required": True,
            "strategy_exit_only": False,
            "risk_per_trade_percent": risk_pct,
            "risk_budget_usdt": risk_budget,
            "uncapped_notional": risk_notional,
            "planned_notional": planned_notional,
            "planned_margin": planned_notional / max(leverage, 1),
            "planned_qty": planned_notional / entry,
            "planned_emergency_loss_usdt": planned_loss,
            "margin_cap_applied": planned_notional + 1e-12 < risk_notional,
        }
    if small_account:
        leverage = int(cfg["small_account_leverage"])
        target_margin = equity * margin_percent / 100.0
        available_margin_cap = free * max(
            0.0,
            min(1.0, _finite(safety_buffer, 0.98)),
        )
        planned_margin = min(target_margin, available_margin_cap)
        planned_notional = planned_margin * leverage
        stop_required = loss_streak > 0
        stop_fraction = emergency_pct / 100.0
        planned_loss = (
            planned_notional * stop_fraction if stop_required else None
        )
        return {
            "sizing_mode": "small_account_loss_ladder",
            "small_account_mode": True,
            "small_account_threshold_usdt": float(
                cfg["small_account_threshold_usdt"]
            ),
            "consecutive_losses": loss_streak,
            "margin_percent": margin_percent,
            "leverage": leverage,
            "emergency_exit_percent": emergency_pct,
            "emergency_stop_required": stop_required,
            "strategy_exit_only": not stop_required,
            "risk_per_trade_percent": None,
            "risk_budget_usdt": planned_loss,
            "uncapped_notional": target_margin * leverage,
            "planned_notional": planned_notional,
            "planned_margin": planned_margin,
            "planned_qty": planned_notional / entry,
            "planned_emergency_loss_usdt": planned_loss,
            "margin_cap_applied": planned_margin + 1e-12 < target_margin,
        }

    risk_pct = float(cfg["risk_per_trade_percent"])
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
        "sizing_mode": "risk_budget",
        "small_account_mode": False,
        "consecutive_losses": loss_streak,
        "margin_percent": None,
        "emergency_stop_required": True,
        "strategy_exit_only": False,
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
    stop_percent=None,
):
    """Return the strategy's immutable emergency-stop anchor for a fill.

    ``stop_percent`` is the per-entry ATR distance from the risk plan; without
    it the configured emergency_exit_percent is used.
    """
    cfg = normalize_ema200_utbot_rsi_config(config)
    entry = _finite(entry_price, 0.0)
    if entry <= 0:
        raise ValueError("entry_price must be positive")
    percent = (
        _finite(stop_percent, 0.0)
        if stop_percent is not None
        else float(cfg["emergency_exit_percent"])
    )
    if percent <= 0:
        raise ValueError("stop distance must be positive")
    fraction = percent / 100.0
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


def ema200_kst_date(now=None):
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference.astimezone(EMA200_KST).date().isoformat()


def apply_ema200_daily_loss_reset(
    daily_realized_pnl,
    reset_payload=None,
    *,
    current_date=None,
):
    """Return today's PnL measured from an explicit manual reset baseline.

    Trade history remains intact.  A reset only changes the daily entry-gate
    baseline for the same Korea calendar day; weekly loss accounting is never
    altered.
    """
    raw_pnl = _finite(daily_realized_pnl, 0.0)
    today = str(current_date or ema200_kst_date())
    payload = reset_payload if isinstance(reset_payload, dict) else {}
    if str(payload.get("date") or "") != today:
        return raw_pnl, 0.0, False
    baseline = _finite(payload.get("baseline_realized_pnl"), 0.0)
    return raw_pnl - baseline, baseline, True


def ema200_consecutive_loss_reset_after(reset_payload=None):
    """Return the validated UTC cutoff for an operator loss-streak reset."""
    if reset_payload is None:
        return None
    if not isinstance(reset_payload, dict):
        raise ValueError("invalid EMA200 consecutive-loss reset state")
    raw = str(reset_payload.get("reset_at") or "").strip()
    if not raw:
        raise ValueError("EMA200 consecutive-loss reset timestamp is missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "invalid EMA200 consecutive-loss reset timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError("EMA200 consecutive-loss reset timestamp must be UTC-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def get_ema200_consecutive_losses(db, reset_payload=None):
    """Read the effective streak, ignoring closes before a manual reset."""
    getter = getattr(db, "get_consecutive_strategy_losses", None)
    if not callable(getter):
        raise RuntimeError("EMA200 consecutive-loss history is unavailable")
    cutoff = ema200_consecutive_loss_reset_after(reset_payload)
    if cutoff is None:
        streak = getter(EMA200_UTBOT_RSI_STRATEGY)
    else:
        streak = getter(EMA200_UTBOT_RSI_STRATEGY, since=cutoff)
    try:
        streak = int(streak)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid EMA200 consecutive-loss count") from exc
    if streak < 0:
        raise ValueError("invalid EMA200 consecutive-loss count")
    return streak, cutoff is not None


__all__ = (
    "EMA200_EXIT_MODES",
    "EMA200_EXIT_ON_ENTRY_TIMEFRAME",
    "EMA200_STRATEGY_REVISION",
    "EMA200_TIMEFRAME_MS",
    "ema200_atr_stop_percent",
    "ema200_effective_exit_timeframe",
    "ema200_exit_timeframe_label",
    "ema200_reentry_allowed_at_ms",
    "EMA200_DEFAULT_ENTRY_TIMEFRAME",
    "EMA200_ENTRY_TIMEFRAMES",
    "EMA200_UTBOT_RSI_STRATEGY",
    "EMA200_UTBOT_RSI_CONFIG_KEY",
    "EMA200_UTBOT_RSI_DISPLAY_NAME",
    "EMA200_UTBOT_KEY_VALUE",
    "EMA200_UTBOT_ATR_PERIOD",
    "EMA200_UTBOT_USE_HEIKIN_ASHI",
    "EMA200_DAILY_LOSS_RESET_STATE_KEY",
    "EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY",
    "default_ema200_utbot_rsi_config",
    "normalize_ema200_utbot_rsi_config",
    "evaluate_ema200_utbot_rsi_entry",
    "build_ema200_utbot_rsi_risk_plan",
    "calculate_ema200_utbot_rsi_emergency_stop_price",
    "evaluate_ema200_utbot_rsi_loss_gate",
    "ema200_kst_date",
    "apply_ema200_daily_loss_reset",
    "ema200_consecutive_loss_reset_after",
    "get_ema200_consecutive_losses",
)

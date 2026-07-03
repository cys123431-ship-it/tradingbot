"""Relative-strength pullback trend entry experiments.

This module is intentionally pure. It never discovers symbols, places orders,
changes risk, or bypasses the existing scanner. Callers must pass scanner
candidates and OHLCV rows that already passed the bot's universe/liquidity
filters.
"""

from dataclasses import dataclass, field
from math import isfinite


ENTRY_STRATEGY_UT_BREAKOUT = "ut_breakout"
ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND = "relative_strength_pullback_trend"


@dataclass(frozen=True)
class PullbackTrendDecision:
    symbol: str
    side: str | None = None
    entry_ready: bool = False
    entry_execution: str = "next_open"
    reason: str = "not_evaluated"
    logs: dict = field(default_factory=dict)
    state_update: dict | None = None

    @property
    def allowed(self):
        return bool(self.entry_ready)


def default_relative_strength_pullback_config():
    return {
        "entry_strategy": ENTRY_STRATEGY_UT_BREAKOUT,
        "relative_strength_pullback_trend_shadow_enabled": True,
        "relative_strength_pullback_trend_live_enabled": False,
        "relative_strength_pullback_trend_paper_enabled": False,
        "trend_htf": "1d",
        "signal_tf": "6h",
        "entry_execution": "next_open",
        "donchian_length": 20,
        "ema_fast": 20,
        "ema_trend": 100,
        "ema_htf_fast": 50,
        "ema_htf": 200,
        "adx_length": 14,
        "adx_threshold": 20.0,
        "relative_strength_short_lookback_bars": 28,
        "relative_strength_long_lookback_bars": 120,
        "relative_strength_block_quantile": 0.30,
        "relative_strength_min_candidates": 4,
        "atr_length": 14,
        "breakout_atr_max": 2.80,
        "breakout_wick_max_ratio": 0.45,
        "extreme_atr_pct": 6.0,
        "pullback_tolerance_atr": 0.50,
        "rebreakout_tolerance_atr": 0.20,
        "exclude_incomplete_live_candle": True,
    }


def resolve_entry_strategy(config=None):
    cfg = dict(default_relative_strength_pullback_config())
    cfg.update(config or {})
    strategy = str(cfg.get("entry_strategy") or ENTRY_STRATEGY_UT_BREAKOUT).strip().lower()
    aliases = {
        "ut": ENTRY_STRATEGY_UT_BREAKOUT,
        "utbreak": ENTRY_STRATEGY_UT_BREAKOUT,
        "ut_breakout": ENTRY_STRATEGY_UT_BREAKOUT,
        "utbot_filtered_breakout_v1": ENTRY_STRATEGY_UT_BREAKOUT,
        "relative_strength_pullback": ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        "relative_strength_pullback_trend": ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        "RelativeStrengthPullbackTrendStrategy".lower(): ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    }
    return aliases.get(strategy, ENTRY_STRATEGY_UT_BREAKOUT)


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if isfinite(parsed) else float(default)


def _row_value(row, key, default=0.0):
    if isinstance(row, dict):
        return _finite_float(row.get(key), default)
    return _finite_float(getattr(row, key, default), default)


def _timestamp_ms(value):
    parsed = _finite_float(value, 0.0)
    if parsed <= 0:
        return 0.0
    return parsed * 1000.0 if parsed < 10_000_000_000 else parsed


def _timeframe_to_ms(tf):
    text = str(tf or "").strip().lower()
    if not text:
        return 0
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    try:
        if text.isdigit():
            return int(text) * multipliers["m"]
        return int(text[:-1]) * multipliers.get(text[-1], 0)
    except (TypeError, ValueError):
        return 0


def _closed_rows(rows, timeframe, config=None, now_ms=None):
    cfg = config or {}
    clean = [dict(row) for row in (rows or []) if isinstance(row, dict)]
    if not clean or not bool(cfg.get("exclude_incomplete_live_candle", True)):
        return clean
    tf_ms = _timeframe_to_ms(timeframe)
    last_ts = _timestamp_ms(clean[-1].get("timestamp"))
    if tf_ms <= 0 or last_ts <= 0:
        return clean
    current_ms = _timestamp_ms(now_ms) if now_ms is not None else 0.0
    if current_ms <= 0:
        return clean
    if last_ts + tf_ms > current_ms:
        return clean[:-1]
    return clean


def _ema(values, length):
    length = max(1, int(length or 1))
    clean = [_finite_float(value) for value in values if isfinite(_finite_float(value, float("nan")))]
    if not clean:
        return None
    alpha = 2.0 / (length + 1.0)
    ema = clean[0]
    for value in clean[1:]:
        ema = alpha * value + (1.0 - alpha) * ema
    return ema


def _true_ranges(rows):
    ranges = []
    for idx, row in enumerate(rows):
        high = _row_value(row, "high")
        low = _row_value(row, "low")
        prev_close = _row_value(rows[idx - 1], "close") if idx > 0 else _row_value(row, "close")
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return ranges


def _atr(rows, length):
    ranges = _true_ranges(rows)
    length = max(1, int(length or 14))
    if not ranges:
        return None
    window = ranges[-length:]
    return sum(window) / max(len(window), 1)


def _adx(rows, length):
    length = max(1, int(length or 14))
    if len(rows) < length + 1:
        return None
    plus_dm = []
    minus_dm = []
    tr = []
    for idx in range(1, len(rows)):
        high = _row_value(rows[idx], "high")
        low = _row_value(rows[idx], "low")
        prev_high = _row_value(rows[idx - 1], "high")
        prev_low = _row_value(rows[idx - 1], "low")
        prev_close = _row_value(rows[idx - 1], "close")
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    tr_sum = sum(tr[-length:])
    if tr_sum <= 0:
        return None
    plus_di = 100.0 * sum(plus_dm[-length:]) / tr_sum
    minus_di = 100.0 * sum(minus_dm[-length:]) / tr_sum
    return 100.0 * abs(plus_di - minus_di) / max(plus_di + minus_di, 1e-12)


def _previous_donchian(rows, length):
    length = max(1, int(length or 20))
    if len(rows) < length + 1:
        return None
    prev = rows[-length - 1:-1]
    high = max(_row_value(row, "high") for row in prev)
    low = min(_row_value(row, "low") for row in prev)
    return {"high": high, "low": low, "mid": (high + low) / 2.0}


def _return_over_rows(rows, lookback):
    lookback = max(1, int(lookback or 1))
    if len(rows) <= lookback:
        return None
    current = _row_value(rows[-1], "close")
    previous = _row_value(rows[-lookback - 1], "close")
    if previous <= 0 or current <= 0:
        return None
    return current / previous - 1.0


def _symbol_from_candidate(candidate):
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, dict):
        return str(
            candidate.get("exchange_symbol")
            or candidate.get("normalized_symbol")
            or candidate.get("symbol")
            or candidate.get("market")
            or ""
        ).strip()
    return ""


def _candidate_symbols(candidates):
    seen = set()
    symbols = []
    for candidate in candidates or []:
        symbol = _symbol_from_candidate(candidate)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _relative_strength_percentiles(symbols, signal_rows_by_symbol, cfg, now_ms=None):
    min_candidates = max(2, int(cfg.get("relative_strength_min_candidates", 4) or 4))
    if len(symbols) < min_candidates:
        return {symbol: {"percentile": None, "reason": "skipped_few_candidates"} for symbol in symbols}
    short_lb = int(cfg.get("relative_strength_short_lookback_bars", 28) or 28)
    long_lb = int(cfg.get("relative_strength_long_lookback_bars", 120) or 120)
    values = []
    for symbol in symbols:
        rows = _closed_rows(signal_rows_by_symbol.get(symbol, []), cfg.get("signal_tf", "6h"), cfg, now_ms)
        short_ret = _return_over_rows(rows, short_lb)
        long_ret = _return_over_rows(rows, long_lb)
        if short_ret is None or long_ret is None:
            continue
        score = 0.60 * long_ret + 0.40 * short_ret
        values.append((symbol, score))
    if len(values) < min_candidates:
        return {symbol: {"percentile": None, "reason": "skipped_insufficient_rs_data"} for symbol in symbols}
    values.sort(key=lambda item: item[1])
    denom = max(len(values) - 1, 1)
    percentiles = {
        symbol: {"percentile": rank / denom, "score": score, "reason": "ok"}
        for rank, (symbol, score) in enumerate(values)
    }
    return {symbol: percentiles.get(symbol, {"percentile": None, "reason": "missing_rs_data"}) for symbol in symbols}


def _trend_side(signal_rows, htf_rows, cfg):
    htf_close = _row_value(htf_rows[-1], "close")
    htf_ema_fast = _ema([_row_value(row, "close") for row in htf_rows], cfg.get("ema_htf_fast", 50))
    htf_ema_slow = _ema([_row_value(row, "close") for row in htf_rows], cfg.get("ema_htf", 200))
    signal_close = _row_value(signal_rows[-1], "close")
    signal_closes = [_row_value(row, "close") for row in signal_rows]
    signal_ema_fast = _ema(signal_closes, cfg.get("ema_fast", 20))
    signal_ema_trend = _ema(signal_closes, cfg.get("ema_trend", 100))
    adx = _adx(signal_rows, cfg.get("adx_length", 14))
    adx_threshold = _finite_float(cfg.get("adx_threshold"), 20.0)
    return {
        "long_htf": bool(htf_ema_slow and htf_close > htf_ema_slow) or bool(htf_ema_fast and htf_ema_slow and htf_ema_fast > htf_ema_slow),
        "short_htf": bool(htf_ema_slow and htf_close < htf_ema_slow) or bool(htf_ema_fast and htf_ema_slow and htf_ema_fast < htf_ema_slow),
        "long_signal": bool(signal_ema_fast and signal_ema_trend and signal_close > signal_ema_fast and signal_close > signal_ema_trend),
        "short_signal": bool(signal_ema_fast and signal_ema_trend and signal_close < signal_ema_fast and signal_close < signal_ema_trend),
        "adx": adx,
        "adx_passed": adx is not None and adx >= adx_threshold,
        "signal_ema_fast": signal_ema_fast,
        "signal_ema_trend": signal_ema_trend,
        "htf_ema_fast": htf_ema_fast,
        "htf_ema_slow": htf_ema_slow,
    }


def _extended_candle_block(row, atr, side, cfg):
    candle_range = max(_row_value(row, "high") - _row_value(row, "low"), 0.0)
    close = _row_value(row, "close")
    open_ = _row_value(row, "open", close)
    if atr and atr > 0 and candle_range / atr > _finite_float(cfg.get("breakout_atr_max"), 2.8):
        return "breakout_candle_too_large"
    if close > 0 and atr and atr > 0 and (atr / close * 100.0) > _finite_float(cfg.get("extreme_atr_pct"), 6.0):
        return "volatility_extreme"
    if candle_range <= 0:
        return None
    if side == "long":
        upper_wick = _row_value(row, "high") - max(open_, close)
        if upper_wick / candle_range > _finite_float(cfg.get("breakout_wick_max_ratio"), 0.45):
            return "upper_wick_too_long"
    if side == "short":
        lower_wick = min(open_, close) - _row_value(row, "low")
        if lower_wick / candle_range > _finite_float(cfg.get("breakout_wick_max_ratio"), 0.45):
            return "lower_wick_too_long"
    return None


def _evaluate_symbol(symbol, signal_rows, htf_rows, rs, state, cfg):
    base_logs = {
        "scanner_passed": True,
        "symbol": symbol,
        "entry_strategy": ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        "entry_execution": cfg.get("entry_execution", "next_open"),
    }
    min_signal = max(int(cfg.get("ema_trend", 100) or 100), int(cfg.get("donchian_length", 20) or 20)) + 2
    min_htf = max(int(cfg.get("ema_htf", 200) or 200), int(cfg.get("ema_htf_fast", 50) or 50)) + 1
    if len(signal_rows) < min_signal or len(htf_rows) < min_htf:
        return PullbackTrendDecision(symbol, reason="insufficient_data", logs={**base_logs, "rejected_reason": "insufficient_data"})

    trend = _trend_side(signal_rows, htf_rows, cfg)
    donchian = _previous_donchian(signal_rows, cfg.get("donchian_length", 20))
    atr = _atr(signal_rows, cfg.get("atr_length", 14))
    row = signal_rows[-1]
    prev = signal_rows[-2]
    close = _row_value(row, "close")
    open_ = _row_value(row, "open", close)
    high = _row_value(row, "high")
    low = _row_value(row, "low")
    if not donchian or not atr or atr <= 0:
        return PullbackTrendDecision(symbol, reason="indicator_not_ready", logs={**base_logs, "rejected_reason": "indicator_not_ready"})

    rs_pct = rs.get("percentile")
    rs_reason = rs.get("reason", "missing")
    q = _finite_float(cfg.get("relative_strength_block_quantile"), 0.30)
    long_rs_passed = rs_pct is None or rs_pct >= q
    short_rs_passed = rs_pct is None or rs_pct <= 1.0 - q
    candidate_sides = []
    if trend["long_htf"] and trend["long_signal"] and trend["adx_passed"] and long_rs_passed:
        candidate_sides.append("long")
    if trend["short_htf"] and trend["short_signal"] and trend["adx_passed"] and short_rs_passed:
        candidate_sides.append("short")

    logs = {
        **base_logs,
        "htf_trend_passed_long": trend["long_htf"],
        "htf_trend_passed_short": trend["short_htf"],
        "signal_tf_trend_passed_long": trend["long_signal"],
        "signal_tf_trend_passed_short": trend["short_signal"],
        "adx_passed": trend["adx_passed"],
        "adx": trend["adx"],
        "relative_strength_percentile": rs_pct,
        "relative_strength_reason": rs_reason,
        "relative_strength_passed_long": long_rs_passed,
        "relative_strength_passed_short": short_rs_passed,
    }

    if not candidate_sides:
        if rs_pct is not None and not long_rs_passed and trend["long_htf"] and trend["long_signal"]:
            reason = "relative_strength_too_weak_for_long"
        elif rs_pct is not None and not short_rs_passed and trend["short_htf"] and trend["short_signal"]:
            reason = "relative_strength_too_strong_for_short"
        elif not trend["adx_passed"]:
            reason = "adx_below_threshold"
        else:
            reason = "trend_filters_not_met"
        return PullbackTrendDecision(symbol, reason=reason, logs={**logs, "rejected_reason": reason})

    state = dict(state or {})
    for side in candidate_sides:
        extension_reason = _extended_candle_block(row, atr, side, cfg)
        if extension_reason:
            return PullbackTrendDecision(symbol, side=side, reason=extension_reason, logs={**logs, "rejected_reason": extension_reason})

        if side == "long":
            breakout_now = close > donchian["high"]
            prior_breakout = state.get("breakout_side") == "long"
            near_pullback = low <= max(trend["signal_ema_fast"], donchian["mid"]) + atr * _finite_float(cfg.get("pullback_tolerance_atr"), 0.50)
            confirmation = close > open_ or close > _row_value(prev, "high")
            rebreakout = high >= max(donchian["high"], _finite_float(state.get("breakout_price"), donchian["high"])) + atr * _finite_float(cfg.get("rebreakout_tolerance_atr"), 0.20)
            if prior_breakout and ((near_pullback and confirmation) or rebreakout):
                return PullbackTrendDecision(
                    symbol,
                    side="long",
                    entry_ready=True,
                    entry_execution=cfg.get("entry_execution", "next_open"),
                    reason="pullback_or_rebreakout_confirmed",
                    logs={**logs, "breakout_confirmed": True, "pullback_confirmed": bool(near_pullback and confirmation), "rebreakout_confirmed": bool(rebreakout)},
                )
            if breakout_now:
                return PullbackTrendDecision(
                    symbol,
                    side="long",
                    entry_ready=False,
                    reason="waiting_for_pullback",
                    logs={**logs, "breakout_confirmed": True, "waiting_for_pullback": True, "rejected_reason": "waiting_for_pullback"},
                    state_update={"breakout_side": "long", "breakout_price": close, "breakout_ts": row.get("timestamp")},
                )
        else:
            breakdown_now = close < donchian["low"]
            prior_breakdown = state.get("breakout_side") == "short"
            near_pullback = high >= min(trend["signal_ema_fast"], donchian["mid"]) - atr * _finite_float(cfg.get("pullback_tolerance_atr"), 0.50)
            confirmation = close < open_ or close < _row_value(prev, "low")
            rebreakdown = low <= min(donchian["low"], _finite_float(state.get("breakout_price"), donchian["low"])) - atr * _finite_float(cfg.get("rebreakout_tolerance_atr"), 0.20)
            if prior_breakdown and ((near_pullback and confirmation) or rebreakdown):
                return PullbackTrendDecision(
                    symbol,
                    side="short",
                    entry_ready=True,
                    entry_execution=cfg.get("entry_execution", "next_open"),
                    reason="pullback_or_rebreakout_confirmed",
                    logs={**logs, "breakout_confirmed": True, "pullback_confirmed": bool(near_pullback and confirmation), "rebreakout_confirmed": bool(rebreakdown)},
                )
            if breakdown_now:
                return PullbackTrendDecision(
                    symbol,
                    side="short",
                    entry_ready=False,
                    reason="waiting_for_pullback",
                    logs={**logs, "breakout_confirmed": True, "waiting_for_pullback": True, "rejected_reason": "waiting_for_pullback"},
                    state_update={"breakout_side": "short", "breakout_price": close, "breakout_ts": row.get("timestamp")},
                )

    return PullbackTrendDecision(symbol, reason="no_breakout_or_pullback", logs={**logs, "rejected_reason": "no_breakout_or_pullback"})


def evaluate_relative_strength_pullback_trend(
    candidates,
    signal_rows_by_symbol,
    htf_rows_by_symbol,
    *,
    state_by_symbol=None,
    config=None,
    now_ms=None,
):
    cfg = default_relative_strength_pullback_config()
    cfg.update(config or {})
    symbols = _candidate_symbols(candidates)
    rs = _relative_strength_percentiles(symbols, signal_rows_by_symbol or {}, cfg, now_ms)
    state_by_symbol = state_by_symbol or {}
    decisions = []
    for symbol in symbols:
        signal_rows = _closed_rows((signal_rows_by_symbol or {}).get(symbol, []), cfg.get("signal_tf", "6h"), cfg, now_ms)
        htf_rows = _closed_rows((htf_rows_by_symbol or {}).get(symbol, []), cfg.get("trend_htf", "1d"), cfg, now_ms)
        decisions.append(_evaluate_symbol(symbol, signal_rows, htf_rows, rs.get(symbol, {}), state_by_symbol.get(symbol), cfg))
    return decisions


def build_relative_strength_pullback_shadow_events(decisions):
    events = []
    for decision in decisions or []:
        if not isinstance(decision, PullbackTrendDecision):
            continue
        events.append({
            "event": "relative_strength_pullback_trend_shadow",
            "symbol": decision.symbol,
            "side": decision.side,
            "entry_ready": decision.entry_ready,
            "entry_execution": decision.entry_execution,
            "reason": decision.reason,
            **dict(decision.logs or {}),
        })
    return events

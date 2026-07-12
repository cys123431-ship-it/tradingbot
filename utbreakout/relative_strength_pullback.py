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
        "signal_tf": "4h",
        "entry_execution": "next_open",
        "forced_direction": None,
        "direction_source": "UTBreakout",
        # Kept for backward-compatible config loading only. Direction is always supplied by UT.
        "require_internal_trend_confirmation": False,
        "donchian_length": 20,
        "ema_pullback": 20,
        "ema_fast": 20,
        "ema_trend": 100,
        "ema_htf_fast": 50,
        "ema_htf": 200,
        "ema_htf_fallback_enabled": True,
        "ema_htf_fallback": 100,
        "adx_length": 14,
        "adx_pass": 20.0,
        "adx_soft_min": 15.0,
        "adx_soft_multiplier": 0.60,
        "adx_threshold": 20.0,
        "relative_strength_short_lookback_bars": 28,
        "relative_strength_long_lookback_bars": 120,
        "relative_strength_block_quantile": 0.20,
        "relative_strength_min_candidates": 2,
        "rs_hard_filter_min_candidates": 10,
        "rs_long_block_bottom_pct": 20.0,
        "rs_short_block_top_pct": 20.0,
        "relative_strength_soft_multiplier": 0.75,
        "atr_length": 14,
        "breakout_atr_max": 2.80,
        "breakout_wick_max_ratio": 0.45,
        "extreme_atr_pct": 6.0,
        "pullback_tolerance_atr": 0.50,
        "pullback_confirmation_lookback": 1,
        "rebreakout_tolerance_atr": 0.20,
        "stale_entry_minutes_4h": 30.0,
        "stale_entry_minutes_6h": 45.0,
        "stale_entry_minutes_1h": 10.0,
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


def _normalize_forced_direction(value):
    text = str(value or "").strip().lower()
    if text in {"long", "buy", "bull", "bullish"}:
        return "long"
    if text in {"short", "sell", "bear", "bearish"}:
        return "short"
    return None


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
    candidate_count = len(symbols)
    if len(symbols) < min_candidates:
        return {
            symbol: {
                "percentile": None,
                "reason": "skipped_few_candidates",
                "candidate_count": candidate_count,
                "ranked_count": 0,
            }
            for symbol in symbols
        }
    short_lb = int(cfg.get("relative_strength_short_lookback_bars", 28) or 28)
    long_lb = int(cfg.get("relative_strength_long_lookback_bars", 120) or 120)
    values = []
    for symbol in symbols:
        rows = _closed_rows(signal_rows_by_symbol.get(symbol, []), cfg.get("signal_tf", "4h"), cfg, now_ms)
        short_ret = _return_over_rows(rows, short_lb)
        long_ret = _return_over_rows(rows, long_lb)
        if short_ret is None or long_ret is None:
            continue
        score = 0.60 * long_ret + 0.40 * short_ret
        values.append((symbol, score))
    if len(values) < min_candidates:
        return {
            symbol: {
                "percentile": None,
                "reason": "skipped_insufficient_rs_data",
                "candidate_count": candidate_count,
                "ranked_count": len(values),
            }
            for symbol in symbols
        }
    values.sort(key=lambda item: item[1])
    denom = max(len(values) - 1, 1)
    percentiles = {
        symbol: {
            "percentile": rank / denom,
            "score": score,
            "reason": "ok",
            "candidate_count": candidate_count,
            "ranked_count": len(values),
        }
        for rank, (symbol, score) in enumerate(values)
    }
    return {
        symbol: percentiles.get(
            symbol,
            {
                "percentile": None,
                "reason": "missing_rs_data",
                "candidate_count": candidate_count,
                "ranked_count": len(values),
            },
        )
        for symbol in symbols
    }


def _trend_side(signal_rows, htf_rows, cfg):
    htf_close = _row_value(htf_rows[-1], "close") if htf_rows else None
    htf_ema_fast = _ema([_row_value(row, "close") for row in htf_rows], cfg.get("ema_htf_fast", 50))
    htf_ema_slow = _ema([_row_value(row, "close") for row in htf_rows], cfg.get("ema_htf", 200))
    signal_close = _row_value(signal_rows[-1], "close")
    signal_closes = [_row_value(row, "close") for row in signal_rows]
    signal_ema_fast = _ema(signal_closes, cfg.get("ema_pullback", cfg.get("ema_fast", 20)))
    signal_ema_trend = _ema(signal_closes, cfg.get("ema_trend", 100))
    adx = _adx(signal_rows, cfg.get("adx_length", 14))
    adx_threshold = _finite_float(cfg.get("adx_pass", cfg.get("adx_threshold")), 20.0)
    return {
        "long_htf": bool(htf_ema_slow and htf_close and htf_close > htf_ema_slow) or bool(htf_ema_fast and htf_ema_slow and htf_ema_fast > htf_ema_slow),
        "short_htf": bool(htf_ema_slow and htf_close and htf_close < htf_ema_slow) or bool(htf_ema_fast and htf_ema_slow and htf_ema_fast < htf_ema_slow),
        "long_signal": bool(signal_ema_fast and signal_ema_trend and signal_close > signal_ema_fast and signal_close > signal_ema_trend),
        "short_signal": bool(signal_ema_fast and signal_ema_trend and signal_close < signal_ema_fast and signal_close < signal_ema_trend),
        "adx": adx,
        "adx_passed": adx is not None and adx >= adx_threshold,
        "adx_threshold": adx_threshold,
        "signal_ema_fast": signal_ema_fast,
        "signal_ema_pullback": signal_ema_fast,
        "signal_ema_trend": signal_ema_trend,
        "htf_ema_fast": htf_ema_fast,
        "htf_ema_slow": htf_ema_slow,
    }


def _pct_to_fraction(value, default_pct):
    parsed = _finite_float(value, default_pct)
    if parsed > 1.0:
        parsed /= 100.0
    return max(0.0, min(1.0, parsed))


def _adx_gate(adx, cfg):
    pass_level = _finite_float(cfg.get("adx_pass", cfg.get("adx_threshold")), 20.0)
    soft_min = _finite_float(cfg.get("adx_soft_min"), 15.0)
    soft_multiplier = max(0.0, min(1.0, _finite_float(cfg.get("adx_soft_multiplier"), 0.60)))
    if adx is None or adx < soft_min:
        return {
            "passed": False,
            "reason": "adx_too_low",
            "risk_multiplier": 0.0,
            "state": "blocked",
        }
    if adx < pass_level:
        return {
            "passed": True,
            "reason": "adx_weak_size_reduced",
            "risk_multiplier": soft_multiplier,
            "state": "reduced",
        }
    return {
        "passed": True,
        "reason": "adx_passed",
        "risk_multiplier": 1.0,
        "state": "passed",
    }


def _relative_strength_gate(side, rs, cfg):
    pct = rs.get("percentile")
    candidate_count = int(rs.get("candidate_count", 0) or 0)
    hard_min = max(2, int(cfg.get("rs_hard_filter_min_candidates", 10) or 10))
    long_block = _pct_to_fraction(cfg.get("rs_long_block_bottom_pct"), 20.0)
    short_block = _pct_to_fraction(cfg.get("rs_short_block_top_pct"), 20.0)
    soft_multiplier = max(0.0, min(1.0, _finite_float(cfg.get("relative_strength_soft_multiplier"), 0.75)))
    if pct is None:
        return {
            "passed": True,
            "reason": rs.get("reason", "missing_rs_data"),
            "risk_multiplier": 1.0,
            "hard_filter_applied": False,
        }
    hard_filter_applies = candidate_count >= hard_min
    adverse = (side == "long" and pct < long_block) or (side == "short" and pct > 1.0 - short_block)
    if hard_filter_applies and adverse:
        return {
            "passed": False,
            "reason": "relative_strength_hard_block",
            "risk_multiplier": 0.0,
            "hard_filter_applied": True,
        }
    if adverse:
        return {
            "passed": True,
            "reason": "relative_strength_size_reduced",
            "risk_multiplier": soft_multiplier,
            "hard_filter_applied": False,
        }
    return {
        "passed": True,
        "reason": "relative_strength_passed",
        "risk_multiplier": 1.0,
        "hard_filter_applied": hard_filter_applies,
    }


def _stale_entry_limit_minutes(signal_tf, cfg):
    text = str(signal_tf or "4h").strip().lower()
    key = "stale_entry_minutes_" + text
    defaults = {"1h": 10.0, "4h": 30.0, "6h": 45.0}
    return max(0.0, _finite_float(cfg.get(key), defaults.get(text, 30.0)))


def _stale_signal_reason(row, cfg, now_ms=None):
    if now_ms is None:
        return None
    tf = str(cfg.get("signal_tf", "4h") or "4h").strip().lower()
    tf_ms = _timeframe_to_ms(tf)
    row_ts = _timestamp_ms(row.get("timestamp") if isinstance(row, dict) else 0.0)
    current_ms = _timestamp_ms(now_ms)
    if tf_ms <= 0 or row_ts <= 0 or current_ms <= 0:
        return None
    close_ts = row_ts + tf_ms
    if current_ms <= close_ts:
        return None
    allowed_ms = _stale_entry_limit_minutes(tf, cfg) * 60_000.0
    if allowed_ms > 0 and current_ms - close_ts > allowed_ms:
        return {
            "reason": "stale_signal",
            "signal_close_ts": close_ts,
            "elapsed_minutes": (current_ms - close_ts) / 60_000.0,
            "allowed_minutes": allowed_ms / 60_000.0,
        }
    return None


def _trend_pullback_setup(side, row, prev, donchian, atr, trend, cfg):
    close = _row_value(row, "close")
    open_ = _row_value(row, "open", close)
    high = _row_value(row, "high")
    low = _row_value(row, "low")
    prev_high = _row_value(prev, "high")
    prev_low = _row_value(prev, "low")
    ema_pullback = trend.get("signal_ema_pullback")
    if ema_pullback is None or donchian is None or atr is None or atr <= 0:
        return False, {"reason": "no_pullback_setup"}
    tolerance = atr * _finite_float(cfg.get("pullback_tolerance_atr"), 0.50)
    if side == "long":
        pullback_target = max(float(ema_pullback), float(donchian["mid"]))
        near_pullback = low <= pullback_target + tolerance
        confirmation = close > open_ or close > prev_high
    else:
        pullback_target = min(float(ema_pullback), float(donchian["mid"]))
        near_pullback = high >= pullback_target - tolerance
        confirmation = close < open_ or close < prev_low
    return bool(near_pullback and confirmation), {
        "reason": "trend_pullback" if near_pullback and confirmation else "no_pullback_setup",
        "pullback_target": pullback_target,
        "pullback_tolerance": tolerance,
        "pullback_near": bool(near_pullback),
        "pullback_confirmation": bool(confirmation),
    }


def _breakout_continuation_setup(side, row, donchian, atr, cfg):
    close = _row_value(row, "close")
    if side == "long":
        breakout_now = close > _finite_float(donchian.get("high"), 0.0)
    else:
        breakout_now = close < _finite_float(donchian.get("low"), 0.0)
    if not breakout_now:
        return False, {"reason": "no_breakout_setup", "breakout_now": False}
    extension_reason = _extended_candle_block(row, atr, side, cfg)
    if extension_reason:
        return False, {"reason": extension_reason, "breakout_now": True}
    return True, {"reason": "breakout_continuation", "breakout_now": True}


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


def _evaluate_symbol(symbol, signal_rows, htf_rows, rs, state, cfg, now_ms=None):
    forced_direction = _normalize_forced_direction(
        cfg.get("forced_direction")
        or cfg.get("rspt_forced_direction")
        or cfg.get("utbreakout_direction")
    )
    direction_source = str(
        cfg.get("direction_source")
        or cfg.get("rspt_direction_source")
        or "UTBreakout"
    )
    base_logs = {
        "scanner_passed": True,
        "symbol": symbol,
        "entry_strategy": ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        "entry_execution": cfg.get("entry_execution", "next_open"),
        "direction_by": direction_source,
        "rspt_direction_source": direction_source,
        "rspt_forced_direction": forced_direction,
    }
    min_signal = max(int(cfg.get("ema_trend", 100) or 100), int(cfg.get("donchian_length", 20) or 20)) + 2
    htf_fast_len = max(1, int(cfg.get("ema_htf_fast", 50) or 50))
    htf_slow_len = max(1, int(cfg.get("ema_htf", 200) or 200))
    min_htf = max(htf_slow_len, htf_fast_len) + 1
    effective_cfg = cfg
    htf_fallback_used = False
    # RSPT no longer owns LONG/SHORT selection. Even if an older runtime config
    # still contains this flag, it must not re-enable the legacy direction gate.
    internal_trend_requested = bool(cfg.get("require_internal_trend_confirmation", False))
    internal_trend_required = False
    if len(signal_rows) < min_signal:
        return PullbackTrendDecision(
            symbol,
            reason="insufficient_data",
            logs={
                **base_logs,
                "rejected_reason": "insufficient_data",
                "signal_rows": len(signal_rows),
                "signal_rows_required": min_signal,
            },
        )
    fallback_len = max(1, int(cfg.get("ema_htf_fallback", 100) or 100))
    fallback_min_htf = max(fallback_len, htf_fast_len) + 1
    if len(htf_rows) < min_htf:
        if (
            bool(cfg.get("ema_htf_fallback_enabled", True))
            and fallback_len < htf_slow_len
            and len(htf_rows) >= fallback_min_htf
        ):
            effective_cfg = dict(cfg)
            effective_cfg["ema_htf"] = fallback_len
            htf_fallback_used = True
            min_htf = fallback_min_htf

    trend = _trend_side(signal_rows, htf_rows, effective_cfg)
    donchian = _previous_donchian(signal_rows, effective_cfg.get("donchian_length", 20))
    atr = _atr(signal_rows, effective_cfg.get("atr_length", 14))
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
    # These trend values remain diagnostic/setup inputs only. They are never
    # allowed to choose or veto the trade direction; UT is authoritative.
    long_trend = bool(trend["long_htf"] and trend["long_signal"])
    short_trend = bool(trend["short_htf"] and trend["short_signal"])
    diagnostic_candidate_sides = []
    if long_trend:
        diagnostic_candidate_sides.append("long")
    if short_trend:
        diagnostic_candidate_sides.append("short")

    logs = {
        **base_logs,
        "htf_trend_passed_long": trend["long_htf"],
        "htf_trend_passed_short": trend["short_htf"],
        "signal_tf_trend_passed_long": trend["long_signal"],
        "signal_tf_trend_passed_short": trend["short_signal"],
        "trend_filter_passed_long": long_trend,
        "trend_filter_passed_short": short_trend,
        "adx_passed": trend["adx_passed"],
        "adx": trend["adx"],
        "adx_pass": trend["adx_threshold"],
        "relative_strength_percentile": rs_pct,
        "relative_strength_reason": rs_reason,
        "relative_strength_candidate_count": rs.get("candidate_count"),
        "relative_strength_ranked_count": rs.get("ranked_count"),
        "rspt_original_candidate_sides": diagnostic_candidate_sides,
        "rspt_ignored_opposite_side": False,
        "rspt_internal_direction_disabled": True,
        "rspt_direction_authority": "UTBreakout",
        "htf_fallback_used": htf_fallback_used,
        "htf_rows": len(htf_rows),
        "htf_rows_required": min_htf,
        "ema_htf_effective": effective_cfg.get("ema_htf", cfg.get("ema_htf", 200)),
        "internal_trend_confirmation_required": False,
        "internal_trend_confirmation_requested_but_ignored": internal_trend_requested,
    }

    if forced_direction not in {"long", "short"}:
        reason = "no_ut_direction"
        return PullbackTrendDecision(symbol, reason=reason, logs={**logs, "rejected_reason": reason})

    logs["rspt_final_side"] = forced_direction
    logs["rspt_ignored_opposite_side"] = any(
        side != forced_direction for side in diagnostic_candidate_sides
    )

    # The only direction entering RSPT quality/setup checks is the shared UT
    # direction. Legacy RSPT trend direction is diagnostic-only.
    candidate_sides = [forced_direction]

    for side in candidate_sides:
        adx_gate = _adx_gate(trend.get("adx"), cfg)
        if not adx_gate["passed"]:
            return PullbackTrendDecision(
                symbol,
                side=side,
                reason="adx_too_low",
                logs={
                    **logs,
                    "adx_state": adx_gate["state"],
                    "adx_size_multiplier": adx_gate["risk_multiplier"],
                    "rejected_reason": "adx_too_low",
                },
            )

        rs_gate = _relative_strength_gate(side, rs, cfg)
        side_logs = {
            **logs,
            "side": side,
            "adx_state": adx_gate["state"],
            "adx_size_multiplier": adx_gate["risk_multiplier"],
            "relative_strength_gate_reason": rs_gate["reason"],
            "relative_strength_hard_filter_applied": rs_gate["hard_filter_applied"],
            "relative_strength_size_multiplier": rs_gate["risk_multiplier"],
            "relative_strength_passed": rs_gate["passed"],
        }
        if not rs_gate["passed"]:
            return PullbackTrendDecision(
                symbol,
                side=side,
                reason="relative_strength_hard_block",
                logs={**side_logs, "rejected_reason": "relative_strength_hard_block"},
            )

        stale = _stale_signal_reason(row, cfg, now_ms)
        if stale:
            return PullbackTrendDecision(
                symbol,
                side=side,
                reason="stale_signal",
                logs={**side_logs, **stale, "rejected_reason": "stale_signal"},
            )

        size_multiplier = min(
            1.0,
            max(0.0, float(adx_gate["risk_multiplier"])),
            max(0.0, float(rs_gate["risk_multiplier"])),
        )
        size_reduction_reasons = [
            reason
            for reason in (adx_gate["reason"], rs_gate["reason"])
            if reason in {"adx_weak_size_reduced", "relative_strength_size_reduced"}
        ]

        breakout_ok, breakout_detail = _breakout_continuation_setup(side, row, donchian, atr, cfg)
        if breakout_ok:
            return PullbackTrendDecision(
                symbol,
                side=side,
                entry_ready=True,
                entry_execution=cfg.get("entry_execution", "next_open"),
                reason="breakout_continuation_confirmed",
                logs={
                    **side_logs,
                    **breakout_detail,
                    "setup_type": "breakout_continuation",
                    "size_multiplier": size_multiplier,
                    "risk_multiplier": size_multiplier,
                    "size_reduction_reasons": size_reduction_reasons,
                    "rejected_reason": None,
                },
            )

        pullback_ok, pullback_detail = _trend_pullback_setup(side, row, prev, donchian, atr, trend, cfg)
        if pullback_ok:
            return PullbackTrendDecision(
                symbol,
                side=side,
                entry_ready=True,
                entry_execution=cfg.get("entry_execution", "next_open"),
                reason="trend_pullback_confirmed",
                logs={
                    **side_logs,
                    **pullback_detail,
                    "setup_type": "trend_pullback",
                    "size_multiplier": size_multiplier,
                    "risk_multiplier": size_multiplier,
                    "size_reduction_reasons": size_reduction_reasons,
                    "rejected_reason": None,
                },
            )

        rejected_reasons = [pullback_detail.get("reason", "no_pullback_setup"), breakout_detail.get("reason", "no_breakout_setup")]
        return PullbackTrendDecision(
            symbol,
            side=side,
            reason="no_entry_setup",
            logs={
                **side_logs,
                "pullback_detail": pullback_detail,
                "breakout_detail": breakout_detail,
                "rejected_reason": ";".join(rejected_reasons),
                "rejected_reasons": rejected_reasons,
            },
        )

    return PullbackTrendDecision(symbol, reason="trend_filter_failed", logs={**logs, "rejected_reason": "trend_filter_failed"})


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
        signal_rows = _closed_rows((signal_rows_by_symbol or {}).get(symbol, []), cfg.get("signal_tf", "4h"), cfg, now_ms)
        htf_rows = _closed_rows((htf_rows_by_symbol or {}).get(symbol, []), cfg.get("trend_htf", "1d"), cfg, now_ms)
        decisions.append(_evaluate_symbol(symbol, signal_rows, htf_rows, rs.get(symbol, {}), state_by_symbol.get(symbol), cfg, now_ms))
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

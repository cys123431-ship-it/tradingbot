"""Session-aware price-pattern overlay for Binance TradFi perpetuals.

The overlay is intentionally deterministic and operates only on completed
OHLCV candles.  It does not turn every named formation into an independent
strategy.  Existing Adaptive Trend entries remain valid, while a confirmed
trend-aligned chart breakout can provide a second, pattern-based entry path.
Candlestick formations are supporting evidence only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from math import isfinite
from statistics import median
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .adaptive_breakout_trend import AdaptiveBreakoutTrendDecision
from .tradfi_small_account import classify_tradfi_instrument


TRADFI_PATTERN_PROFILE_VERSION = "tradfi_pattern_profile_v2_session_anchor"


def default_tradfi_pattern_profile_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "profile_version": TRADFI_PATTERN_PROFILE_VERSION,
        "pattern_timeframe": "1h",
        "higher_timeframe": "4h",
        "daily_timeframe": "1d",
        "chart_lookback_bars": 24,
        "retest_lookback_bars": 3,
        "minimum_pattern_score": 4.0,
        "minimum_early_momentum": 0.10,
        "minimum_volume_ratio": 0.80,
        "breakout_buffer_atr": 0.05,
        "gap_shock_atr": 1.50,
        "regular_session_required_for_pattern_entry": True,
        "maximum_leverage": 10,
        "leverage_steps": (5, 8, 10),
    }


def normalize_tradfi_pattern_profile_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = default_tradfi_pattern_profile_config()
    result = dict(defaults)
    if isinstance(config, Mapping):
        result.update(dict(config))
    result["enabled"] = _as_bool(result.get("enabled"), True)
    result["profile_version"] = TRADFI_PATTERN_PROFILE_VERSION
    result["chart_lookback_bars"] = _bounded_int(
        result.get("chart_lookback_bars"), 12, 72, 24
    )
    result["retest_lookback_bars"] = _bounded_int(
        result.get("retest_lookback_bars"), 1, 6, 3
    )
    result["minimum_pattern_score"] = _bounded_float(
        result.get("minimum_pattern_score"), 2.0, 10.0, 4.0
    )
    result["minimum_early_momentum"] = _bounded_float(
        result.get("minimum_early_momentum"), 0.05, 0.50, 0.10
    )
    result["minimum_volume_ratio"] = _bounded_float(
        result.get("minimum_volume_ratio"), 0.0, 3.0, 0.80
    )
    result["breakout_buffer_atr"] = _bounded_float(
        result.get("breakout_buffer_atr"), 0.0, 0.50, 0.05
    )
    result["gap_shock_atr"] = _bounded_float(
        result.get("gap_shock_atr"), 0.50, 5.0, 1.50
    )
    result["regular_session_required_for_pattern_entry"] = _as_bool(
        result.get("regular_session_required_for_pattern_entry"), True
    )
    result["maximum_leverage"] = _bounded_int(
        result.get("maximum_leverage"), 5, 10, 10
    )
    try:
        steps = sorted(
            {
                int(float(value))
                for value in result.get("leverage_steps", (5, 8, 10))
                if 5 <= int(float(value)) <= result["maximum_leverage"]
            }
        )
    except (TypeError, ValueError):
        steps = []
    if 5 not in steps:
        steps.insert(0, 5)
    if result["maximum_leverage"] not in steps:
        steps.append(result["maximum_leverage"])
    result["leverage_steps"] = tuple(sorted(set(steps)))
    return result


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _bounded_float(value: Any, lower: float, upper: float, default: float) -> float:
    parsed = _finite(value, default)
    return max(lower, min(upper, float(parsed if parsed is not None else default)))


def _bounded_int(value: Any, lower: int, upper: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _clean_rows(rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for row in rows or ():
        close = _finite(row.get("close"))
        open_price = _finite(row.get("open"), close)
        high = _finite(row.get("high"))
        low = _finite(row.get("low"))
        volume = _finite(row.get("volume"), 0.0)
        if None in (close, open_price, high, low):
            continue
        if close <= 0 or high <= 0 or low <= 0 or high < low:
            continue
        cleaned.append(
            {
                "timestamp": row.get("timestamp"),
                "open": float(open_price),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": max(0.0, float(volume or 0.0)),
            }
        )
    return cleaned


def _ema(values: Sequence[float], period: int) -> list[float]:
    alpha = 2.0 / (max(2, int(period)) + 1.0)
    current = float(values[0])
    result: list[float] = []
    for value in values:
        current = alpha * float(value) + (1.0 - alpha) * current
        result.append(current)
    return result


def _atr(rows: Sequence[Mapping[str, Any]], period: int = 20) -> float | None:
    if len(rows) < max(3, period + 1):
        return None
    values: list[float] = []
    for index in range(1, len(rows)):
        previous_close = float(rows[index - 1]["close"])
        high = float(rows[index]["high"])
        low = float(rows[index]["low"])
        values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    window = values[-period:]
    return sum(window) / len(window) if window else None


def _linear_slope(values: Sequence[float]) -> float:
    count = len(values)
    if count < 2:
        return 0.0
    midpoint = (count - 1) / 2.0
    denominator = sum((index - midpoint) ** 2 for index in range(count))
    if denominator <= 0:
        return 0.0
    mean_value = sum(float(value) for value in values) / count
    return sum(
        (index - midpoint) * (float(value) - mean_value)
        for index, value in enumerate(values)
    ) / denominator


def _trend_direction(rows: Sequence[Mapping[str, Any]] | None) -> str | None:
    candles = _clean_rows(rows)
    if len(candles) < 52:
        return None
    closes = [float(row["close"]) for row in candles]
    fast = _ema(closes, 20)
    slow = _ema(closes, 50)
    slope_index = max(0, len(slow) - 5)
    if closes[-1] > fast[-1] > slow[-1] and slow[-1] > slow[slope_index]:
        return "long"
    if closes[-1] < fast[-1] < slow[-1] and slow[-1] < slow[slope_index]:
        return "short"
    return None


def tradfi_trend_direction(
    rows: Sequence[Mapping[str, Any]] | None,
) -> str | None:
    """Public wrapper used by the live benchmark-context cache."""

    return _trend_direction(rows)


def _candle_patterns(candles: Sequence[Mapping[str, Any]], atr_value: float) -> dict[str, list[str]]:
    result = {"long": [], "short": []}
    if len(candles) < 4 or atr_value <= 0:
        return result
    previous = candles[-2]
    current = candles[-1]
    prev_body_low = min(float(previous["open"]), float(previous["close"]))
    prev_body_high = max(float(previous["open"]), float(previous["close"]))
    body_low = min(float(current["open"]), float(current["close"]))
    body_high = max(float(current["open"]), float(current["close"]))
    body = max(abs(float(current["close"]) - float(current["open"])), atr_value * 0.03)
    lower_wick = body_low - float(current["low"])
    upper_wick = float(current["high"]) - body_high

    if previous["close"] < previous["open"] and current["close"] > current["open"]:
        if body_low <= prev_body_low and body_high >= prev_body_high:
            result["long"].append("bullish_engulfing")
        elif body_low >= prev_body_low and body_high <= prev_body_high:
            result["long"].append("bullish_harami")
    if previous["close"] > previous["open"] and current["close"] < current["open"]:
        if body_low <= prev_body_low and body_high >= prev_body_high:
            result["short"].append("bearish_engulfing")
        elif body_low >= prev_body_low and body_high <= prev_body_high:
            result["short"].append("bearish_harami")
    if lower_wick >= body * 2.0 and upper_wick <= body * 0.8:
        result["long"].append("hammer_pinbar")
    if upper_wick >= body * 2.0 and lower_wick <= body * 0.8:
        result["short"].append("shooting_star")

    first, middle, last = candles[-3], candles[-2], candles[-1]
    first_body = abs(float(first["close"]) - float(first["open"]))
    middle_body = abs(float(middle["close"]) - float(middle["open"]))
    if first_body >= atr_value * 0.35 and middle_body <= first_body * 0.55:
        midpoint = (float(first["open"]) + float(first["close"])) / 2.0
        if first["close"] < first["open"] and last["close"] > last["open"] and last["close"] > midpoint:
            result["long"].append("morning_star")
        if first["close"] > first["open"] and last["close"] < last["open"] and last["close"] < midpoint:
            result["short"].append("evening_star")

    mother, inside, false_break, confirmation = candles[-4:]
    is_inside = inside["high"] < mother["high"] and inside["low"] > mother["low"]
    if is_inside:
        if false_break["low"] < inside["low"] and confirmation["close"] > inside["high"]:
            result["long"].append("bullish_hikkake")
        if false_break["high"] > inside["high"] and confirmation["close"] < inside["low"]:
            result["short"].append("bearish_hikkake")
    return result


def _pivot_points(candles: Sequence[Mapping[str, Any]], order: int = 2) -> list[tuple[int, str, float]]:
    pivots: list[tuple[int, str, float]] = []
    for index in range(order, len(candles) - order):
        high = float(candles[index]["high"])
        low = float(candles[index]["low"])
        neighbors = candles[index - order:index] + candles[index + 1:index + order + 1]
        if all(high > float(row["high"]) for row in neighbors):
            pivots.append((index, "high", high))
        if all(low < float(row["low"]) for row in neighbors):
            pivots.append((index, "low", low))
    pivots.sort(key=lambda item: item[0])
    return pivots


def _reversal_chart_patterns(
    candles: Sequence[Mapping[str, Any]], atr_value: float
) -> dict[str, list[str]]:
    result = {"long": [], "short": []}
    if len(candles) < 24 or atr_value <= 0:
        return result
    sample = list(candles[-48:])
    pivots = _pivot_points(sample)
    highs = [item for item in pivots if item[1] == "high"]
    lows = [item for item in pivots if item[1] == "low"]
    close = float(sample[-1]["close"])

    if len(lows) >= 2:
        first, second = lows[-2], lows[-1]
        between_highs = [item[2] for item in highs if first[0] < item[0] < second[0]]
        if (
            second[0] - first[0] >= 4
            and abs(second[2] - first[2]) <= atr_value * 0.65
            and between_highs
            and max(between_highs) - min(first[2], second[2]) >= atr_value * 1.25
            and close > max(between_highs)
        ):
            result["long"].append("double_bottom_breakout")
    if len(highs) >= 2:
        first, second = highs[-2], highs[-1]
        between_lows = [item[2] for item in lows if first[0] < item[0] < second[0]]
        if (
            second[0] - first[0] >= 4
            and abs(second[2] - first[2]) <= atr_value * 0.65
            and between_lows
            and max(first[2], second[2]) - min(between_lows) >= atr_value * 1.25
            and close < min(between_lows)
        ):
            result["short"].append("double_top_breakdown")

    alternating = pivots[-7:]
    for start in range(max(0, len(alternating) - 5), len(alternating) - 4):
        group = alternating[start:start + 5]
        kinds = [item[1] for item in group]
        values = [item[2] for item in group]
        if kinds == ["high", "low", "high", "low", "high"]:
            shoulders_close = abs(values[0] - values[4]) <= atr_value * 0.85
            head_clear = values[2] >= max(values[0], values[4]) + atr_value * 0.45
            neckline = (values[1] + values[3]) / 2.0
            if shoulders_close and head_clear and close < neckline:
                result["short"].append("head_and_shoulders_breakdown")
        if kinds == ["low", "high", "low", "high", "low"]:
            shoulders_close = abs(values[0] - values[4]) <= atr_value * 0.85
            head_clear = values[2] <= min(values[0], values[4]) - atr_value * 0.45
            neckline = (values[1] + values[3]) / 2.0
            if shoulders_close and head_clear and close > neckline:
                result["long"].append("inverse_head_and_shoulders_breakout")
    return result


def _continuation_chart_patterns(
    candles: Sequence[Mapping[str, Any]], atr_value: float, cfg: Mapping[str, Any]
) -> dict[str, list[str]]:
    result = {"long": [], "short": []}
    lookback = int(cfg["chart_lookback_bars"])
    if len(candles) < lookback + 8 or atr_value <= 0:
        return result
    current = candles[-1]
    close = float(current["close"])
    buffer_value = atr_value * float(cfg["breakout_buffer_atr"])
    prior = list(candles[-lookback - 1:-1])
    prior_high = max(float(row["high"]) for row in prior)
    prior_low = min(float(row["low"]) for row in prior)
    width_atr = (prior_high - prior_low) / atr_value
    if close > prior_high + buffer_value:
        result["long"].append(
            "rectangle_breakout" if width_atr <= 7.0 else "range_breakout"
        )
    if close < prior_low - buffer_value:
        result["short"].append(
            "rectangle_breakdown" if width_atr <= 7.0 else "range_breakdown"
        )

    retest_bars = int(cfg["retest_lookback_bars"])
    for age in range(1, retest_bars + 1):
        breakout_index = len(candles) - 1 - age
        if breakout_index <= lookback:
            continue
        history = candles[breakout_index - lookback:breakout_index]
        level_high = max(float(row["high"]) for row in history)
        level_low = min(float(row["low"]) for row in history)
        breakout = candles[breakout_index]
        if (
            float(breakout["close"]) > level_high + buffer_value
            and float(current["low"]) <= level_high + atr_value * 0.25
            and close > level_high
        ):
            result["long"].append("breakout_retest_hold")
            break
        if (
            float(breakout["close"]) < level_low - buffer_value
            and float(current["high"]) >= level_low - atr_value * 0.25
            and close < level_low
        ):
            result["short"].append("breakdown_retest_hold")
            break

    formation = list(candles[-19:-1])
    first_half = formation[:9]
    second_half = formation[9:]
    if first_half and second_half:
        high_contracting = max(row["high"] for row in second_half) < max(row["high"] for row in first_half)
        low_contracting = min(row["low"] for row in second_half) > min(row["low"] for row in first_half)
        second_high = max(float(row["high"]) for row in second_half)
        second_low = min(float(row["low"]) for row in second_half)
        if high_contracting and low_contracting and close > second_high + buffer_value:
            result["long"].append("triangle_breakout")
        if high_contracting and low_contracting and close < second_low - buffer_value:
            result["short"].append("triangle_breakdown")

    impulse = list(candles[-16:-7])
    consolidation = list(candles[-7:-1])
    if impulse and consolidation:
        impulse_move = float(impulse[-1]["close"]) - float(impulse[0]["open"])
        consolidation_high = max(float(row["high"]) for row in consolidation)
        consolidation_low = min(float(row["low"]) for row in consolidation)
        consolidation_range = consolidation_high - consolidation_low
        consolidation_slope = _linear_slope([float(row["close"]) for row in consolidation])
        if (
            impulse_move >= atr_value * 2.0
            and consolidation_range <= atr_value * 3.5
            and consolidation_slope <= atr_value * 0.10
            and close > consolidation_high + buffer_value
        ):
            result["long"].append("bull_flag_breakout")
        if (
            impulse_move <= -atr_value * 2.0
            and consolidation_range <= atr_value * 3.5
            and consolidation_slope >= -atr_value * 0.10
            and close < consolidation_low - buffer_value
        ):
            result["short"].append("bear_flag_breakdown")
    return result


def _merge_patterns(*groups: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    merged = {"long": [], "short": []}
    for group in groups:
        for side in merged:
            for name in group.get(side, ()):
                if name not in merged[side]:
                    merged[side].append(str(name))
    return merged


def _timestamp_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        raw = float(value)
        if not isfinite(raw):
            return None
        if abs(raw) > 100_000_000_000:
            raw /= 1000.0
        try:
            parsed = datetime.fromtimestamp(raw, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    elif value not in (None, ""):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _session_adjusted_volume_ratio(
    candles: Sequence[Mapping[str, Any]],
    session_status: Mapping[str, Any] | None,
) -> tuple[float, str, int]:
    volumes = [float(row.get("volume") or 0.0) for row in candles]
    fallback = volumes[-49:-1] if len(volumes) >= 49 else volumes[:-1]
    baseline = median(fallback) if fallback else 0.0
    method = "rolling_median"
    samples = len(fallback)

    timezone_name = str((session_status or {}).get("timezone") or "UTC")
    latest_dt = _timestamp_datetime(candles[-1].get("timestamp")) if candles else None
    if latest_dt is not None:
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            tz = timezone.utc
        latest_local = latest_dt.astimezone(tz)
        latest_bucket = (latest_local.hour, latest_local.minute)
        comparable = []
        for row in candles[:-1]:
            row_dt = _timestamp_datetime(row.get("timestamp"))
            if row_dt is None:
                continue
            local = row_dt.astimezone(tz)
            if (local.hour, local.minute) == latest_bucket:
                comparable.append(float(row.get("volume") or 0.0))
        if len(comparable) >= 3:
            baseline = median(comparable[-20:])
            samples = min(20, len(comparable))
            method = "same_session_time_bucket"

    latest_volume = volumes[-1] if volumes else 0.0
    ratio = latest_volume / max(baseline, 1e-9) if baseline > 0 else 1.0
    return ratio, method, samples


def _gap_shock(candles: Sequence[Mapping[str, Any]], atr_value: float, threshold: float) -> bool:
    if len(candles) < 2 or atr_value <= 0:
        return False
    latest = candles[-1]
    previous_close = float(candles[-2]["close"])
    gap = abs(float(latest["open"]) - previous_close)
    true_range = max(
        float(latest["high"]) - float(latest["low"]),
        abs(float(latest["high"]) - previous_close),
        abs(float(latest["low"]) - previous_close),
    )
    # A large opening discontinuity is suspicious at the configured threshold.
    # Intrabar range gets more room because a genuine trend breakout is itself
    # expected to be wider than an ordinary candle.
    return (
        gap / atr_value > threshold
        or true_range / atr_value > threshold * 2.5
    )


def evaluate_tradfi_pattern_profile(
    rows: Sequence[Mapping[str, Any]] | None,
    base_decision: AdaptiveBreakoutTrendDecision,
    *,
    symbol: str | None = None,
    underlying_type: str | None = None,
    higher_timeframe_rows: Sequence[Mapping[str, Any]] | None = None,
    daily_rows: Sequence[Mapping[str, Any]] | None = None,
    benchmark_directions: Mapping[str, str | None] | None = None,
    session_status: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> AdaptiveBreakoutTrendDecision:
    """Apply a TradFi-only OR entry overlay to an Adaptive Trend decision."""

    cfg = normalize_tradfi_pattern_profile_config(config)
    metrics = dict(base_decision.metrics or {})
    metrics.update(
        {
            "tradfi_profile_version": TRADFI_PATTERN_PROFILE_VERSION,
            "tradfi_pattern_profile_applied": bool(cfg["enabled"]),
        }
    )
    if not cfg["enabled"]:
        return replace(base_decision, metrics=metrics)

    candles = _clean_rows(rows)
    atr_value = _atr(candles, 20)
    if len(candles) < int(cfg["chart_lookback_bars"]) + 8 or not atr_value:
        metrics["tradfi_pattern_data_ready"] = False
        return replace(base_decision, metrics=metrics)

    candle_patterns = _candle_patterns(candles, atr_value)
    chart_patterns = _merge_patterns(
        _continuation_chart_patterns(candles, atr_value, cfg),
        _reversal_chart_patterns(candles, atr_value),
    )
    volume_ratio, volume_baseline_method, volume_baseline_samples = (
        _session_adjusted_volume_ratio(candles, session_status)
    )
    htf_direction = _trend_direction(higher_timeframe_rows)
    daily_direction = _trend_direction(daily_rows)
    benchmark_directions = dict(benchmark_directions or {})
    benchmark_votes = [
        str(value).lower()
        for value in benchmark_directions.values()
        if str(value or "").lower() in {"long", "short"}
    ]
    session_open = bool((session_status or {}).get("open"))
    gap_shock = _gap_shock(candles, atr_value, float(cfg["gap_shock_atr"]))

    side_scores: dict[str, float] = {}
    for side in ("long", "short"):
        chart_score = min(6.0, len(chart_patterns[side]) * 2.5)
        candle_score = min(2.0, len(candle_patterns[side]) * 1.0)
        trend_score = 0.0
        available_trends = [value for value in (htf_direction, daily_direction) if value]
        if available_trends and all(value == side for value in available_trends):
            trend_score = 2.0
        elif side in available_trends and not any(value != side for value in available_trends):
            trend_score = 1.0
        benchmark_score = 0.0
        if benchmark_votes and not any(value != side for value in benchmark_votes):
            benchmark_score = 1.0
        volume_score = 1.0 if volume_ratio >= float(cfg["minimum_volume_ratio"]) else 0.0
        side_scores[side] = chart_score + candle_score + trend_score + benchmark_score + volume_score

    base_side = str(base_decision.side or "").lower()
    chart_side_score = side_scores.get(base_side, 0.0)
    opposing_side = "short" if base_side == "long" else "long"
    opposing_score = side_scores.get(opposing_side, 0.0)
    available_trends = [value for value in (htf_direction, daily_direction) if value]
    higher_trend_aligned = bool(
        base_side in {"long", "short"}
        and available_trends
        and base_side in available_trends
        and not any(value != base_side for value in available_trends)
    )
    benchmark_conflict = bool(
        benchmark_votes
        and base_side in {"long", "short"}
        and all(value != base_side for value in benchmark_votes)
    )
    opposing_chart = bool(
        base_side in {"long", "short"}
        and chart_patterns[opposing_side]
        and opposing_score > chart_side_score + 1.0
    )
    session_allows_pattern = bool(
        session_open or not cfg["regular_session_required_for_pattern_entry"]
    )
    pattern_evidence_trusted = bool(
        session_allows_pattern
        and volume_ratio >= float(cfg["minimum_volume_ratio"])
        and not opposing_chart
        and not gap_shock
    )
    pattern_entry_allowed = bool(
        base_side in {"long", "short"}
        and chart_patterns[base_side]
        and chart_side_score >= float(cfg["minimum_pattern_score"])
        and volume_ratio >= float(cfg["minimum_volume_ratio"])
        and higher_trend_aligned
        and session_allows_pattern
        and not opposing_chart
        and not gap_shock
        and bool(metrics.get("ema_aligned"))
        and abs(float(metrics.get("weighted_momentum", 0.0) or 0.0))
        >= float(cfg["minimum_early_momentum"])
    )
    metrics.update(
        {
            "tradfi_pattern_data_ready": True,
            "tradfi_chart_patterns": chart_patterns,
            "tradfi_candle_patterns": candle_patterns,
            "tradfi_pattern_side_scores": side_scores,
            "tradfi_pattern_score": chart_side_score,
            "tradfi_pattern_entry_allowed": pattern_entry_allowed,
            "tradfi_pattern_evidence_trusted": pattern_evidence_trusted,
            "tradfi_higher_timeframe_direction": htf_direction,
            "tradfi_daily_direction": daily_direction,
            "tradfi_benchmark_directions": benchmark_directions,
            "tradfi_benchmark_conflict": benchmark_conflict,
            "tradfi_regular_session_open": session_open,
            "tradfi_session_reason": (session_status or {}).get("reason"),
            "tradfi_gap_shock": gap_shock,
            "tradfi_volume_ratio": volume_ratio,
            "tradfi_volume_baseline_method": volume_baseline_method,
            "tradfi_volume_baseline_samples": volume_baseline_samples,
            "tradfi_opposing_chart": opposing_chart,
            "tradfi_benchmark_corroborating_only": True,
            "tradfi_instrument_profile": classify_tradfi_instrument(
                symbol,
                underlying_type,
            ),
        }
    )

    if base_decision.allowed:
        bonus = 0.0
        if chart_patterns.get(base_side) and pattern_evidence_trusted:
            bonus += min(5.0, len(chart_patterns[base_side]) * 2.0)
        if candle_patterns.get(base_side) and pattern_evidence_trusted:
            bonus += min(2.0, len(candle_patterns[base_side]))
        if higher_trend_aligned:
            bonus += 1.0
        score = min(100.0, float(base_decision.score) + bonus)
        tier = "elite" if score >= 88.0 else "strong" if score >= 76.0 else "base"
        metrics["score"] = score
        metrics["risk_tier"] = tier
        if pattern_entry_allowed:
            metrics["tradfi_entry_mode"] = "base_plus_pattern_confirmation"
        else:
            metrics["tradfi_entry_mode"] = "base_adaptive_trend"
        names = chart_patterns.get(base_side) if pattern_evidence_trusted else []
        suffix = f"; TradFi patterns={','.join(names)}" if names else "; TradFi pattern neutral"
        return AdaptiveBreakoutTrendDecision(
            allowed=True,
            side=base_decision.side,
            score=score,
            risk_multiplier=base_decision.risk_multiplier,
            reason=base_decision.reason + suffix,
            metrics=metrics,
        )

    soft_wait_reasons = {
        "waiting_for_weighted_trend_entry",
        "score_below_threshold",
        "momentum_strength_too_low",
    }
    if pattern_entry_allowed and base_decision.reason in soft_wait_reasons:
        score = min(100.0, max(68.0, 62.0 + chart_side_score * 3.0))
        tier = "elite" if score >= 88.0 else "strong" if score >= 76.0 else "base"
        metrics.update(
            {
                "score": score,
                "risk_tier": tier,
                "risk_multiplier": 1.0,
                "tradfi_entry_mode": "pattern_or_entry",
            }
        )
        names = chart_patterns.get(base_side) or []
        return AdaptiveBreakoutTrendDecision(
            allowed=True,
            side=base_decision.side,
            score=score,
            risk_multiplier=1.0,
            reason=(
                f"TradFi pattern OR {base_side}: {','.join(names)} "
                f"score={score:.1f} htf={htf_direction or 'N/A'} "
                f"daily={daily_direction or 'N/A'}"
            ),
            metrics=metrics,
        )

    metrics["tradfi_entry_mode"] = "waiting"
    return replace(base_decision, metrics=metrics)


__all__ = (
    "TRADFI_PATTERN_PROFILE_VERSION",
    "default_tradfi_pattern_profile_config",
    "normalize_tradfi_pattern_profile_config",
    "evaluate_tradfi_pattern_profile",
    "tradfi_trend_direction",
)

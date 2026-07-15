from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


M_TREND_STRATEGY = "multi_timeframe_trend_v1"
M_TREND_TIMEFRAMES = ("15m", "1h", "4h")


def default_multi_timeframe_trend_config() -> dict[str, Any]:
    """Broad, low-parameter defaults for the live multi-timeframe trend strategy."""

    return {
        "enabled": True,
        "live_enabled": False,
        "timeframes": list(M_TREND_TIMEFRAMES),
        "donchian_lookbacks": {"15m": 32, "1h": 24, "4h": 18},
        "bias_fast_ema": 20,
        "bias_slow_ema": 50,
        "atr_period": 14,
        "single_timeframe_risk_multiplier": 0.45,
        "two_timeframe_risk_multiplier": 0.75,
        "three_timeframe_risk_multiplier": 1.0,
        "stop_atr_multiplier": 1.50,
        "take_profit_r_multiple": 3.0,
        "trailing_atr_multiplier": 3.0,
        "time_stop_bars": 48,
    }


@dataclass(frozen=True)
class MultiTimeframeTrendDecision:
    allowed: bool
    side: str | None
    score: float
    risk_multiplier: float
    reason: str
    confirmations: tuple[str, ...]
    metrics: dict[str, Any]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_rows(rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, float]]:
    cleaned: list[dict[str, float]] = []
    for row in rows or []:
        values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close")}
        if any(value is None for value in values.values()):
            continue
        cleaned.append({key: float(value) for key, value in values.items() if value is not None})
    return cleaned


def _ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period <= 1:
        return None
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def _atr(rows: Sequence[Mapping[str, float]], period: int) -> float | None:
    if len(rows) < period + 1:
        return None
    ranges: list[float] = []
    for index in range(1, len(rows)):
        high = float(rows[index]["high"])
        low = float(rows[index]["low"])
        previous_close = float(rows[index - 1]["close"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(ranges) < period:
        return None
    return sum(ranges[-period:]) / period


def _fresh_donchian_breakout(rows: Sequence[Mapping[str, float]], lookback: int) -> dict[str, Any]:
    if len(rows) < lookback + 2:
        return {"side": None, "reason": "insufficient_data"}
    last = rows[-1]
    previous = rows[-2]
    previous_window = rows[-(lookback + 1):-1]
    prior_window = rows[-(lookback + 2):-2]
    upper = max(float(row["high"]) for row in previous_window)
    lower = min(float(row["low"]) for row in previous_window)
    prior_upper = max(float(row["high"]) for row in prior_window)
    prior_lower = min(float(row["low"]) for row in prior_window)
    close = float(last["close"])
    previous_close = float(previous["close"])
    side = None
    if close > upper and previous_close <= prior_upper:
        side = "long"
    elif close < lower and previous_close >= prior_lower:
        side = "short"
    return {
        "side": side,
        "close": close,
        "upper": upper,
        "lower": lower,
        "breakout_distance": (
            close - upper if side == "long" else lower - close if side == "short" else 0.0
        ),
    }


def evaluate_multi_timeframe_trend(
    frames: Mapping[str, Sequence[Mapping[str, Any]]],
    config: Mapping[str, Any] | None = None,
) -> MultiTimeframeTrendDecision:
    cfg = default_multi_timeframe_trend_config()
    if isinstance(config, Mapping):
        cfg.update(dict(config))
    lookbacks = dict(default_multi_timeframe_trend_config()["donchian_lookbacks"])
    if isinstance(cfg.get("donchian_lookbacks"), Mapping):
        lookbacks.update(dict(cfg["donchian_lookbacks"]))

    cleaned = {timeframe: _clean_rows(frames.get(timeframe)) for timeframe in M_TREND_TIMEFRAMES}
    fast_period = max(2, int(cfg.get("bias_fast_ema", 20) or 20))
    slow_period = max(fast_period + 1, int(cfg.get("bias_slow_ema", 50) or 50))
    htf_closes = [row["close"] for row in cleaned["4h"]]
    fast_ema = _ema(htf_closes, fast_period)
    slow_ema = _ema(htf_closes, slow_period)
    if fast_ema is None or slow_ema is None or not htf_closes:
        return MultiTimeframeTrendDecision(
            False, None, 0.0, 0.0, "M-TREND waiting: insufficient completed 4h candles",
            (), {"data_ready": False},
        )

    latest_htf_close = htf_closes[-1]
    bias = (
        "long" if latest_htf_close > fast_ema > slow_ema
        else "short" if latest_htf_close < fast_ema < slow_ema
        else None
    )
    breakout_metrics: dict[str, dict[str, Any]] = {}
    breakout_sides: dict[str, str] = {}
    atr_period = max(2, int(cfg.get("atr_period", 14) or 14))
    for timeframe in M_TREND_TIMEFRAMES:
        breakout = _fresh_donchian_breakout(
            cleaned[timeframe],
            max(2, int(lookbacks.get(timeframe, 20) or 20)),
        )
        atr_value = _atr(cleaned[timeframe], atr_period)
        breakout["atr"] = atr_value
        distance = float(breakout.get("breakout_distance") or 0.0)
        breakout["breakout_atr"] = distance / atr_value if atr_value and atr_value > 0 else 0.0
        breakout_metrics[timeframe] = breakout
        if breakout.get("side") in {"long", "short"}:
            breakout_sides[timeframe] = str(breakout["side"])

    unique_sides = set(breakout_sides.values())
    metrics = {
        "data_ready": True,
        "bias": bias,
        "bias_fast_ema": fast_ema,
        "bias_slow_ema": slow_ema,
        "bias_close": latest_htf_close,
        "timeframes": breakout_metrics,
    }
    if len(unique_sides) > 1:
        return MultiTimeframeTrendDecision(
            False, None, 0.0, 0.0, "M-TREND waiting: timeframe breakout direction conflict",
            (), metrics,
        )
    if not breakout_sides:
        return MultiTimeframeTrendDecision(
            False, None, 0.0, 0.0, "M-TREND waiting: no fresh Donchian breakout",
            (), metrics,
        )
    side = next(iter(unique_sides))
    if bias is None:
        return MultiTimeframeTrendDecision(
            False, None, 0.0, 0.0, "M-TREND waiting: 4h EMA trend is neutral",
            (), metrics,
        )
    if side != bias:
        return MultiTimeframeTrendDecision(
            False, None, 0.0, 0.0,
            f"M-TREND waiting: {side.upper()} breakout conflicts with 4h {bias.upper()} bias",
            (), metrics,
        )

    confirmations = tuple(
        timeframe for timeframe in M_TREND_TIMEFRAMES if breakout_sides.get(timeframe) == side
    )
    count = len(confirmations)
    risk_multiplier = {
        1: float(cfg.get("single_timeframe_risk_multiplier", 0.45) or 0.45),
        2: float(cfg.get("two_timeframe_risk_multiplier", 0.75) or 0.75),
        3: float(cfg.get("three_timeframe_risk_multiplier", 1.0) or 1.0),
    }.get(count, 0.0)
    risk_multiplier = max(0.0, min(1.0, risk_multiplier))
    normalized_strength = sum(
        min(1.0, max(0.0, float(breakout_metrics[timeframe].get("breakout_atr") or 0.0)))
        for timeframe in confirmations
    )
    score = min(100.0, 45.0 + 15.0 * count + 10.0 * normalized_strength)
    metrics["confirmation_count"] = count
    metrics["confirmations"] = list(confirmations)
    return MultiTimeframeTrendDecision(
        True,
        side,
        score,
        risk_multiplier,
        (
            f"M-TREND {side.upper()}: {count}/3 fresh breakout confirmation(s) "
            f"aligned with 4h EMA trend"
        ),
        confirmations,
        metrics,
    )

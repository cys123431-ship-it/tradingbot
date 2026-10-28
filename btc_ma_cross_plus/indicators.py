"""Pure indicator and filter helpers for the /macross strategy (no I/O).

Candle rows are ``[open_ms, open, high, low, close, volume]``.  Every function
here runs on live candles and on historical klines, so the same code drives
the bot and the local backtest.
"""

from __future__ import annotations

from math import floor


def sma(values, period):
    """Simple moving average aligned with ``values`` (None before the first full window)."""
    if len(values) < period:
        return []
    out = [None] * (period - 1)
    window = sum(values[:period])
    out.append(window / period)
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out.append(window / period)
    return out


def true_range(rows):
    """True range per candle (first candle uses high-low)."""
    out = []
    prev_close = None
    for row in rows:
        high, low, close = row[2], row[3], row[4]
        if prev_close is None:
            out.append(high - low)
        else:
            out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return out


def atr(rows, period=14):
    """Wilder-smoothed ATR aligned with ``rows`` (None until ``period`` values)."""
    tr = true_range(rows)
    if len(tr) < period:
        return []
    out = [None] * (period - 1)
    current = sum(tr[:period]) / period
    out.append(current)
    for value in tr[period:]:
        current = (current * (period - 1) + value) / period
        out.append(current)
    return out


def trend_efficiency(values, period):
    """Kaufman efficiency ratio: |net move| / sum(|bar-to-bar moves|) over ``period``."""
    out = [None] * len(values)
    for i in range(period, len(values)):
        window = values[i - period:i + 1]
        net = abs(window[-1] - window[0])
        path = sum(abs(window[j] - window[j - 1]) for j in range(1, len(window)))
        out[i] = (net / path) if path else 0.0
    return out


def donchian(rows, period):
    """(upper, lower) of the previous ``period`` candles, excluding the current one."""
    upper, lower = [None] * len(rows), [None] * len(rows)
    for i in range(period, len(rows)):
        window = rows[i - period:i]
        upper[i] = max(r[2] for r in window)
        lower[i] = min(r[3] for r in window)
    return upper, lower


def avg_volume(rows, period):
    out = [None] * len(rows)
    for i in range(period - 1, len(rows)):
        window = rows[i - period + 1:i + 1]
        out[i] = sum(r[5] for r in window) / period
    return out


def floor_to_step(value, step):
    if not step or step <= 0:
        return value
    return floor(float(value) / float(step)) * float(step)


def adx(rows, period=14):
    """Wilder ADX aligned with ``rows`` (None until enough history)."""
    count = len(rows)
    if count < period * 2 + 1:
        return []
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, count):
        up = rows[i][2] - rows[i - 1][2]
        down = rows[i - 1][3] - rows[i][3]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        prev_close = rows[i - 1][4]
        tr.append(max(rows[i][2] - rows[i][3],
                      abs(rows[i][2] - prev_close),
                      abs(rows[i][3] - prev_close)))

    def _wilder(values):
        smoothed = [sum(values[:period])]
        for value in values[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + value)
        return smoothed

    sm_tr = _wilder(tr)
    sm_plus = _wilder(plus_dm)
    sm_minus = _wilder(minus_dm)
    dx = []
    for i in range(len(sm_tr)):
        if not sm_tr[i]:
            dx.append(0.0)
            continue
        di_plus = 100.0 * sm_plus[i] / sm_tr[i]
        di_minus = 100.0 * sm_minus[i] / sm_tr[i]
        total = di_plus + di_minus
        dx.append(100.0 * abs(di_plus - di_minus) / total if total else 0.0)
    if len(dx) < period:
        return []
    smoothed = [sum(dx[:period]) / period]
    for value in dx[period:]:
        smoothed.append((smoothed[-1] * (period - 1) + value) / period)
    return [None] * (count - len(smoothed)) + smoothed


def volatility_target_scale(atr_value, close, target_pct, min_scale, max_scale):
    """Risk multiplier that keeps a fixed % risk per trade (feature C).

    ``target_pct`` is the desired ATR-to-price ratio; calmer markets get a
    bigger size (capped) and wilder markets a smaller one.
    """
    try:
        close = float(close)
        atr_value = float(atr_value)
        target_pct = float(target_pct)
    except (TypeError, ValueError):
        return 1.0
    if close <= 0 or atr_value <= 0 or target_pct <= 0:
        return 1.0
    current = atr_value / close
    if current <= 0:
        return 1.0
    return max(float(min_scale), min(float(max_scale), target_pct / current))


def chandelier_stop(side, highest_high, lowest_low, atr_value, multiplier, previous=None,
                    breakeven=None):
    """ATR chandelier trailing stop that never loosens (feature D)."""
    try:
        atr_value = float(atr_value)
    except (TypeError, ValueError):
        return None
    if atr_value <= 0:
        return None
    if str(side).upper() == "LONG":
        if highest_high is None:
            return None
        candidate = float(highest_high) - atr_value * float(multiplier)
        if breakeven is not None:
            candidate = max(candidate, float(breakeven))
        if previous is not None:
            candidate = max(candidate, float(previous))
        return candidate
    if lowest_low is None:
        return None
    candidate = float(lowest_low) + atr_value * float(multiplier)
    if breakeven is not None:
        candidate = min(candidate, float(breakeven))
    if previous is not None:
        candidate = min(candidate, float(previous))
    return candidate


__all__ = (
    "adx",
    "atr",
    "avg_volume",
    "chandelier_stop",
    "donchian",
    "floor_to_step",
    "sma",
    "trend_efficiency",
    "true_range",
    "volatility_target_scale",
)

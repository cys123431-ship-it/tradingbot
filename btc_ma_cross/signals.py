"""SMA3 / SMA200 cross on closed candles and the margin-ROI profit lock (pure, backtestable)."""

from __future__ import annotations

from math import floor

from btc_ema_pullback.signals import TIMEFRAME_MS, closed_candles


def sma(values, period):
    if len(values) < period:
        return []
    out = [None] * (period - 1)
    window = sum(values[:period])
    out.append(window / period)
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out.append(window / period)
    return out


def market_is_flat(closed, cfg, lookback=100):
    """True for a frozen / untraded market (a horizontal-line chart)."""
    recent = closed[-lookback:]
    if not recent:
        return True
    ranges = sorted((r[2] - r[3]) / r[4] for r in recent if r[4] > 0)
    if not ranges:
        return True
    median = ranges[len(ranges) // 2]
    zero_share = sum(1 for value in ranges if value <= 0) / len(ranges)
    return (median < float(cfg.get("min_median_bar_range_pct", 0.0005))
            or zero_share > float(cfg.get("max_zero_range_bar_share", 0.1)))


def evaluate_cross(rows, cfg, now_ms, symbol="BTCUSDT", ccxt_symbol=None):
    """Cross of SMA(fast) over SMA(slow) on the last *closed* candle.

    ``side`` is set only on a fresh cross; ``trend_side`` always reports
    whether SMA(fast) is above (LONG) or below (SHORT) SMA(slow).
    """
    timeframe = cfg["timeframe"]
    fast_p, slow_p = int(cfg["fast_period"]), int(cfg["slow_period"])
    closed = closed_candles(rows, timeframe, now_ms)
    result = {
        "symbol": symbol,
        "market_id": symbol,
        "ccxt_symbol": ccxt_symbol or f"{symbol[:-4]}/USDT:USDT",
        "timeframe": timeframe,
        "timestamp_ms": int(now_ms),
        "side": None,
        "trend_side": None,
        "stale": False,
    }
    if len(closed) < slow_p + 1:
        result["skip_reason"] = f"INSUFFICIENT_CANDLES:{len(closed)}<{slow_p + 1}"
        return result
    if market_is_flat(closed, cfg):
        result["flat"] = True
        result["skip_reason"] = "FLAT_MARKET"
        return result
    closes = [r[4] for r in closed]
    fast, slow = sma(closes, fast_p), sma(closes, slow_p)
    bar = closed[-1]
    result.update(
        bar_open_ms=bar[0],
        close=bar[4],
        sma_fast=fast[-1],
        sma_slow=slow[-1],
        sma_fast_prev=fast[-2],
        sma_slow_prev=slow[-2],
        signal_age_seconds=(int(now_ms) - bar[0] - TIMEFRAME_MS[timeframe]) / 1000.0,
    )
    result["trend_side"] = "LONG" if fast[-1] > slow[-1] else ("SHORT" if fast[-1] < slow[-1] else None)
    result["stale"] = result["signal_age_seconds"] > float(cfg["signal_max_age_seconds"])
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
        side = "LONG"
    elif fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
        side = "SHORT"
    else:
        result["skip_reason"] = "NO_CROSS_ABOVE" if fast[-1] > slow[-1] else "NO_CROSS_BELOW"
        return result
    if result["stale"]:
        result["skip_reason"] = "SIGNAL_STALE"
        return result
    result["side"] = side
    result["skip_reason"] = ""
    result["signal_id"] = f"{symbol}:SMA{fast_p}/{slow_p}:{timeframe}:{side}:{bar[0]}"
    return result


def margin_roi(side, entry_price, price, leverage):
    direction = 1.0 if side == "LONG" else -1.0
    return direction * (float(price) / float(entry_price) - 1.0) * float(leverage) * 100.0


def profit_lock_target(side, entry_price, best_price, leverage, start, step, gap):
    """(step reached, locked ROI, stop price) for the best price seen, or None.

    With start 5 / step 5 / gap 1: ROI +5% keeps +4%, +10% keeps +9%,
    +15% keeps +14%, ... (margin ROI = price move x leverage).
    """
    peak = margin_roi(side, entry_price, best_price, leverage)
    start, step, gap = float(start), float(step), float(gap)
    if peak + 1e-9 < start:
        return None
    reached = start + floor((peak - start) / step + 1e-9) * step
    locked = reached - gap
    direction = 1.0 if side == "LONG" else -1.0
    price = float(entry_price) * (1.0 + direction * locked / (float(leverage) * 100.0))
    return reached, locked, price


def emergency_stop_price(side, entry_price, leverage, roi_percent):
    if not roi_percent:
        return None
    direction = 1.0 if side == "LONG" else -1.0
    return float(entry_price) * (1.0 - direction * float(roi_percent) / (float(leverage) * 100.0))


__all__ = (
    "emergency_stop_price",
    "evaluate_cross",
    "market_is_flat",
    "margin_roi",
    "profit_lock_target",
    "sma",
)

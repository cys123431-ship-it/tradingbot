"""Pure candidate ranking helpers for the EMA200 2H strategy.

The scanner owns market-data I/O and order execution.  This module only turns
already-valid strategy signals into comparable metrics and a deterministic
ranking, which keeps selection testable without touching exchange state.
"""

from __future__ import annotations

from math import isfinite


EMA200_CANDIDATE_TIMEFRAME_MS = 2 * 60 * 60 * 1000
EMA200_AUXILIARY_UT_WEIGHTS = {"15m": 4.0, "30m": 6.0, "1h": 8.0}


def auxiliary_ut_score(side, biases):
    """Bounded ranking bonus; unknown data is neutral and never vetoes entry."""
    breakdown = {}
    for timeframe, weight in EMA200_AUXILIARY_UT_WEIGHTS.items():
        bias = str((biases or {}).get(timeframe) or '').lower()
        breakdown[timeframe] = (
            weight if bias == side else -weight if bias in {'long', 'short'} else 0.0
        )
    return sum(breakdown.values()), breakdown


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return parsed if isfinite(parsed) else float(default)


def _completed_rows(ohlcv):
    rows = list(ohlcv or [])
    # Binance OHLCV includes the currently forming candle as the last row.
    # Strategy comparisons must use completed 2-hour candles only.
    return [row for row in rows[:-1] if isinstance(row, (list, tuple)) and len(row) >= 6]


def _average_true_range(rows, length=14):
    if len(rows) < 2:
        return 0.0
    true_ranges = []
    start = max(1, len(rows) - max(2, int(length)))
    for index in range(start, len(rows)):
        high = _finite_float(rows[index][2])
        low = _finite_float(rows[index][3])
        previous_close = _finite_float(rows[index - 1][4])
        true_ranges.append(
            max(
                max(0.0, high - low),
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )
    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def build_ema200_candidate(*, symbol, side, detail, ohlcv, market_price=None):
    """Build ranking metrics from one already-valid completed-candle signal."""

    side = str(side or "").strip().lower()
    if side not in {"long", "short"}:
        raise ValueError("EMA200 candidate side must be long or short")

    detail = dict(detail or {})
    rows = _completed_rows(ohlcv)
    direction = 1.0 if side == "long" else -1.0
    close_price = _finite_float(
        detail.get("closed_candle_close"),
        rows[-1][4] if rows else 0.0,
    )
    ema_now = _finite_float(detail.get("ema200"))
    ema_previous = _finite_float(detail.get("ema200_previous"), ema_now)
    prev_rsi = _finite_float(detail.get("prev_rsi"), 50.0)
    curr_rsi = _finite_float(detail.get("curr_rsi"), 50.0)
    closed_candle_ts = int(
        _finite_float(
            detail.get("closed_candle_ts"),
            rows[-1][0] if rows else 0,
        )
    )
    ut_signal_ts = int(_finite_float(detail.get("ut_last_signal_ts"), 0.0))
    ut_age_bars = (
        max(0.0, (closed_candle_ts - ut_signal_ts) / EMA200_CANDIDATE_TIMEFRAME_MS)
        if closed_candle_ts > 0 and ut_signal_ts > 0
        else float("inf")
    )

    ema_slope_percent = 0.0
    if ema_previous:
        ema_slope_percent = direction * (ema_now - ema_previous) / abs(ema_previous) * 100.0
    rsi_momentum = direction * (curr_rsi - prev_rsi)
    atr = _average_true_range(rows)
    extension_atr = abs(close_price - ema_now) / atr if atr > 0 else 0.0
    quote_volume_24h = sum(
        max(0.0, _finite_float(row[4])) * max(0.0, _finite_float(row[5]))
        for row in rows[-12:]
    )

    return {
        "symbol": str(symbol or ""),
        "side": side,
        "closed_candle_ts": closed_candle_ts,
        "market_price": _finite_float(market_price, close_price),
        "ut_age_bars": ut_age_bars,
        "rsi_momentum": max(0.0, rsi_momentum),
        "ema_slope_percent": ema_slope_percent,
        "quote_volume_24h": quote_volume_24h,
        "extension_atr": extension_atr,
        "detail": detail,
    }


def _relative_quality(values, value, *, lower_is_better=False):
    finite_values = [float(item) for item in values if isfinite(float(item))]
    if not finite_values or not isfinite(float(value)):
        return 0.0
    low = min(finite_values)
    high = max(finite_values)
    if high <= low:
        return 0.5
    quality = (float(value) - low) / (high - low)
    return 1.0 - quality if lower_is_better else quality


def rank_ema200_candidates(candidates):
    """Return a deterministic best-first ranking without changing eligibility.

    Scores are relative within the same completed 2-hour candle.  They select
    among signals that already passed the strategy; they do not create or veto
    a signal.  This distinction keeps the original EMA/UT/RSI contract intact.
    """

    prepared = [dict(item) for item in candidates or [] if isinstance(item, dict)]
    if not prepared:
        return []

    ages = [_finite_float(item.get("ut_age_bars"), float("inf")) for item in prepared]
    rsi_moves = [_finite_float(item.get("rsi_momentum")) for item in prepared]
    ema_slopes = [_finite_float(item.get("ema_slope_percent")) for item in prepared]
    liquidities = [_finite_float(item.get("quote_volume_24h")) for item in prepared]

    ranked = []
    for item in prepared:
        recency = 40.0 * _relative_quality(
            ages,
            _finite_float(item.get("ut_age_bars"), float("inf")),
            lower_is_better=True,
        )
        rsi = 25.0 * _relative_quality(
            rsi_moves,
            _finite_float(item.get("rsi_momentum")),
        )
        ema = 20.0 * _relative_quality(
            ema_slopes,
            _finite_float(item.get("ema_slope_percent")),
        )
        liquidity = 15.0 * _relative_quality(
            liquidities,
            _finite_float(item.get("quote_volume_24h")),
        )
        # This is a ranking penalty, not an entry filter.  A signal more than
        # three ATR away from EMA200 is relatively less desirable because it is
        # more likely to be a late chase, but it remains eligible if it is the
        # only valid candidate.
        extension = max(0.0, _finite_float(item.get("extension_atr")))
        extension_penalty = min(15.0, max(0.0, extension - 3.0) * 5.0)
        auxiliary, auxiliary_breakdown = auxiliary_ut_score(
            item.get('side'), item.get('auxiliary_ut_biases')
        )
        score = max(0.0, recency + rsi + ema + liquidity - extension_penalty + auxiliary)
        item["score"] = round(score, 4)
        item["score_breakdown"] = {
            "ut_recency": round(recency, 4),
            "rsi_momentum": round(rsi, 4),
            "ema_slope": round(ema, 4),
            "liquidity": round(liquidity, 4),
            "extension_penalty": round(extension_penalty, 4),
            "auxiliary_ut": auxiliary,
            "auxiliary_ut_by_timeframe": auxiliary_breakdown,
        }
        ranked.append(item)

    ranked.sort(
        key=lambda item: (
            -_finite_float(item.get("score")),
            _finite_float(item.get("ut_age_bars"), float("inf")),
            -_finite_float(item.get("quote_volume_24h")),
            str(item.get("symbol") or ""),
        )
    )
    for index, item in enumerate(ranked, 1):
        item["rank"] = index
        item["candidate_count"] = len(ranked)
    return ranked


__all__ = (
    "EMA200_CANDIDATE_TIMEFRAME_MS",
    "build_ema200_candidate",
    "rank_ema200_candidates",
)

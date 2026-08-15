"""Regime-adaptive, long-premium option selection helpers."""

from __future__ import annotations

import math
import statistics
import time


def _f(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value, low=-1.0, high=1.0):
    return min(float(high), max(float(low), float(value)))


def _ema(values, period):
    values = [_f(value) for value in values]
    if not values:
        return 0.0
    alpha = 2.0 / (max(1, int(period)) + 1.0)
    result = values[0]
    for value in values[1:]:
        result = (alpha * value) + ((1.0 - alpha) * result)
    return result


def _atr(rows, period=14):
    if len(rows) < 2:
        return 0.0
    trs = []
    previous = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        trs.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    sample = trs[-max(2, int(period)) :]
    return statistics.fmean(sample) if sample else 0.0


def _realized_volatility(closes, periods_per_year=24 * 365):
    returns = []
    for previous, current in zip(closes, closes[1:]):
        if previous > 0 and current > 0:
            returns.append(math.log(current / previous))
    sample = returns[-72:]
    if len(sample) < 12:
        return 0.0
    return statistics.pstdev(sample) * math.sqrt(periods_per_year)


def evaluate_underlying_trend(fast_rows, slow_rows, cfg=None):
    cfg = cfg or {}
    if len(fast_rows or []) < 110 or len(slow_rows or []) < 55:
        return {"accepted": False, "reason": "UNDERLYING_CANDLES_INSUFFICIENT"}
    fast_closes = [_f(row[4]) for row in fast_rows]
    slow_closes = [_f(row[4]) for row in slow_rows]
    price = fast_closes[-1]
    atr = max(_atr(fast_rows, 14), price * 0.001)

    fast_component = _clamp((_ema(fast_closes, 12) - _ema(fast_closes, 26)) / atr / 2.0)
    medium_component = _clamp((_ema(fast_closes, 20) - _ema(fast_closes, 50)) / atr / 3.0)
    slow_component = _clamp((price - _ema(fast_closes, 100)) / atr / 5.0)
    recent = fast_closes[-21:-1]
    recent_low, recent_high = min(recent), max(recent)
    span = max(recent_high - recent_low, atr)
    breakout_component = _clamp(((price - recent_low) / span - 0.5) * 2.0)
    slow_tf_component = _clamp(
        (_ema(slow_closes, 20) - _ema(slow_closes, 50))
        / max(_atr(slow_rows, 14), price * 0.002)
        / 3.0
    )
    score = (
        0.20 * fast_component
        + 0.20 * medium_component
        + 0.15 * slow_component
        + 0.20 * breakout_component
        + 0.25 * slow_tf_component
    )
    threshold = _f(cfg.get("min_abs_signal"), 0.52)
    accepted = abs(score) >= threshold
    direction = "CALL" if score > 0 else "PUT"
    return {
        "accepted": accepted,
        "reason": "TREND_SCORE_READY" if accepted else "TREND_SCORE_BELOW_THRESHOLD",
        "score": float(score),
        "direction": direction,
        "spot_price": float(price),
        "atr_pct": float(atr / price) if price > 0 else 0.0,
        "realized_volatility": float(_realized_volatility(fast_closes)),
        "signal_bar_ts": int(_f(fast_rows[-1][0])),
        "components": {
            "fast": fast_component,
            "medium": medium_component,
            "slow": slow_component,
            "breakout": breakout_component,
            "slow_timeframe": slow_tf_component,
        },
    }


def _lot_filter(contract):
    min_qty = _f(contract.get("minQty"))
    step = 0.0
    for item in contract.get("filters") or []:
        if str(item.get("filterType") or "").upper() == "LOT_SIZE":
            min_qty = _f(item.get("minQty"), min_qty)
            step = _f(item.get("stepSize"))
    return min_qty, step or min_qty


def _price_filter(contract):
    tick = 0.0
    for item in contract.get("filters") or []:
        if str(item.get("filterType") or "").upper() == "PRICE_FILTER":
            tick = _f(item.get("tickSize"))
    return tick


def shortlist_option_contracts(exchange_info, underlying, direction, spot_price, cfg=None, now_ms=None):
    cfg = cfg or {}
    now_ms = int(now_ms or time.time() * 1000)
    direction = str(direction or "").upper()
    spot = _f(spot_price)
    rows = []
    for contract in (exchange_info or {}).get("optionSymbols") or []:
        if str(contract.get("underlying") or "").upper() != str(underlying).upper():
            continue
        if str(contract.get("side") or "").upper() != direction:
            continue
        if str(contract.get("status") or "").upper() != "TRADING":
            continue
        if str(contract.get("underlyingType") or "CRYPTO").upper() != "CRYPTO":
            continue
        if str(contract.get("contractType") or "CRYPTO_OPTIONS").upper() != "CRYPTO_OPTIONS":
            continue
        expiry = int(_f(contract.get("expiryDate")))
        dte = (expiry - now_ms) / 86_400_000.0
        if dte < _f(cfg.get("min_dte_days"), 1.0) or dte > _f(cfg.get("max_dte_days"), 14.0):
            continue
        strike = _f(contract.get("strikePrice"))
        if spot <= 0 or strike <= 0:
            continue
        otm = (strike / spot - 1.0) if direction == "CALL" else (spot / strike - 1.0)
        if otm < -0.08 or otm > 0.18:
            continue
        min_qty, step = _lot_filter(contract)
        row = dict(contract)
        row.update(
            {
                "dte_days": dte,
                "otm_pct": otm,
                "min_qty": min_qty,
                "step_size": step,
                "tick_size": _price_filter(contract),
            }
        )
        target_dte = _f(cfg.get("target_dte_days"), 5.0)
        target_otm = _f(cfg.get("target_otm_pct"), 0.02)
        row["shortlist_distance"] = abs(dte - target_dte) / max(target_dte, 1.0) + abs(otm - target_otm) * 8.0
        rows.append(row)
    rows.sort(key=lambda item: (item["shortlist_distance"], item["dte_days"]))
    return rows[: int(cfg.get("max_candidates_per_underlying", 6) or 6)]


def _first_row(payload):
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


def score_option_contract(contract, mark_payload, ticker_payload, depth_payload, signal, cfg=None):
    cfg = cfg or {}
    mark = _first_row(mark_payload)
    ticker = _first_row(ticker_payload)
    bids = list((depth_payload or {}).get("bids") or [])
    asks = list((depth_payload or {}).get("asks") or [])
    bid = _f(bids[0][0]) if bids and len(bids[0]) >= 2 else _f(ticker.get("bidPrice"))
    ask = _f(asks[0][0]) if asks and len(asks[0]) >= 2 else _f(ticker.get("askPrice"))
    if bid <= 0 or ask <= 0 or ask <= bid:
        return {"accepted": False, "reason": "OPTION_ORDERBOOK_NOT_TRADEABLE"}
    mid = (bid + ask) / 2.0
    spread = (ask - bid) / mid if mid > 0 else 1.0
    if spread > _f(cfg.get("max_spread_pct"), 0.18):
        return {"accepted": False, "reason": "OPTION_SPREAD_TOO_WIDE", "spread_pct": spread}
    delta = abs(_f(mark.get("delta")))
    if delta < _f(cfg.get("min_abs_delta"), 0.25) or delta > _f(cfg.get("max_abs_delta"), 0.70):
        return {"accepted": False, "reason": "OPTION_DELTA_OUTSIDE_TARGET", "delta": delta}
    quote_volume = _f(ticker.get("amount"))
    if quote_volume < _f(cfg.get("min_quote_volume_usdt"), 50.0):
        return {"accepted": False, "reason": "OPTION_VOLUME_TOO_LOW", "quote_volume": quote_volume}
    iv = _f(mark.get("markIV"))
    realized = max(0.01, _f(signal.get("realized_volatility")))
    iv_ratio = iv / realized if iv > 0 else 999.0
    signal_strength = abs(_f(signal.get("score")))
    if iv_ratio > _f(cfg.get("max_iv_to_realized"), 1.60) and signal_strength < _f(cfg.get("strong_signal"), 0.75):
        return {"accepted": False, "reason": "OPTION_IV_TOO_EXPENSIVE", "iv_to_realized": iv_ratio}
    depth_qty = min(_f(bids[0][1]), _f(asks[0][1])) if bids and asks else 0.0
    score = (
        signal_strength * 45.0
        + (1.0 - min(spread / max(_f(cfg.get("max_spread_pct"), 0.18), 0.01), 1.0)) * 20.0
        + (1.0 - min(abs(delta - 0.45) / 0.30, 1.0)) * 15.0
        + (1.0 - min(max(iv_ratio - 0.8, 0.0) / 1.5, 1.0)) * 15.0
        + min(depth_qty / 10.0, 1.0) * 5.0
    )
    return {
        "accepted": True,
        "reason": "OPTION_CONTRACT_READY",
        "score": score,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_pct": spread,
        "delta": delta,
        "mark_iv": iv,
        "iv_to_realized": iv_ratio,
        "quote_volume": quote_volume,
        "depth_qty": depth_qty,
    }


__all__ = (
    "evaluate_underlying_trend",
    "score_option_contract",
    "shortlist_option_contracts",
)

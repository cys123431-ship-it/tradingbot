"""Adaptive, long-premium option signal and contract selection helpers."""

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


def _true_ranges(rows):
    if len(rows or []) < 2:
        return []
    trs = []
    previous = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        trs.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    return trs


def _atr(rows, period=14):
    trs = _true_ranges(rows)
    sample = trs[-max(2, int(period)) :]
    return statistics.fmean(sample) if sample else 0.0


def _realized_volatility(closes, periods_per_year=24 * 365, sample_size=72):
    returns = []
    for previous, current in zip(closes, closes[1:]):
        if previous > 0 and current > 0:
            returns.append(math.log(current / previous))
    sample = returns[-max(12, int(sample_size)) :]
    if len(sample) < 12:
        return 0.0
    return statistics.pstdev(sample) * math.sqrt(periods_per_year)


def evaluate_underlying_trend(fast_rows, slow_rows, cfg=None):
    """Score the retained 1h + 4h trend structure without all-or-nothing sub-filters."""
    cfg = cfg or {}
    if len(fast_rows or []) < 110 or len(slow_rows or []) < 55:
        return {"accepted": False, "reason": "UNDERLYING_CANDLES_INSUFFICIENT", "strategy": "ADAPTIVE_TREND"}
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
    threshold = _f(cfg.get("min_abs_signal"), 0.46)
    accepted = abs(score) >= threshold
    direction = "CALL" if score > 0 else "PUT"
    return {
        "accepted": accepted,
        "reason": "ADAPTIVE_TREND_READY" if accepted else "TREND_SCORE_BELOW_THRESHOLD",
        "strategy": "ADAPTIVE_TREND",
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


def evaluate_low_iv_squeeze(fast_rows, slow_rows, cfg=None):
    """Detect compressed realized volatility followed by a confirmed range break.

    IV itself is evaluated later at the option-contract layer; this function
    identifies the underlying setup using realized range/ATR compression,
    momentum and volume expansion so a single Bollinger-style condition cannot
    create an entry by itself.
    """
    cfg = cfg or {}
    if len(fast_rows or []) < 90 or len(slow_rows or []) < 30:
        return {"accepted": False, "reason": "SQUEEZE_CANDLES_INSUFFICIENT", "strategy": "LOW_IV_SQUEEZE"}

    closes = [_f(row[4]) for row in fast_rows]
    highs = [_f(row[2]) for row in fast_rows]
    lows = [_f(row[3]) for row in fast_rows]
    volumes = [_f(row[5]) if len(row) > 5 else 0.0 for row in fast_rows]
    price = closes[-1]
    if price <= 0:
        return {"accepted": False, "reason": "SQUEEZE_PRICE_INVALID", "strategy": "LOW_IV_SQUEEZE"}

    trs = _true_ranges(fast_rows)
    recent_atr = statistics.fmean(trs[-12:]) if len(trs) >= 12 else _atr(fast_rows, 14)
    baseline_sample = trs[-72:-12] if len(trs) >= 72 else trs[:-12]
    baseline_atr = statistics.fmean(baseline_sample) if baseline_sample else recent_atr
    compression_ratio = recent_atr / max(baseline_atr, price * 1e-6)

    short_rv = _realized_volatility(closes, sample_size=18)
    medium_rv = _realized_volatility(closes, sample_size=72)
    rv_ratio = short_rv / max(medium_rv, 0.01)

    prior_high = max(highs[-21:-1])
    prior_low = min(lows[-21:-1])
    up_break = (price - prior_high) / max(recent_atr, price * 0.001)
    down_break = (prior_low - price) / max(recent_atr, price * 0.001)
    direction = "CALL" if up_break >= down_break else "PUT"
    breakout_strength = max(up_break, down_break)

    volume_baseline = statistics.fmean(volumes[-21:-1]) if any(volumes[-21:-1]) else 0.0
    volume_ratio = volumes[-1] / max(volume_baseline, 1e-12) if volume_baseline > 0 else 1.0
    momentum = (price / closes[-4] - 1.0) if len(closes) >= 4 and closes[-4] > 0 else 0.0
    momentum_in_atr = abs(momentum * price) / max(recent_atr, price * 0.001)

    slow_closes = [_f(row[4]) for row in slow_rows]
    slow_bias = _clamp(
        (_ema(slow_closes, 12) - _ema(slow_closes, 26))
        / max(_atr(slow_rows, 14), price * 0.002)
        / 2.5
    )
    direction_sign = 1.0 if direction == "CALL" else -1.0
    alignment = _clamp(slow_bias * direction_sign, -1.0, 1.0)

    compression_limit = _f(cfg.get("squeeze_compression_ratio"), 0.78)
    volume_min = _f(cfg.get("squeeze_volume_multiplier"), 1.15)
    compressed = compression_ratio <= compression_limit and rv_ratio <= 0.90
    breakout = breakout_strength >= 0.15
    confirmed = volume_ratio >= volume_min or momentum_in_atr >= 0.65

    compression_score = _clamp((compression_limit - compression_ratio) / max(compression_limit, 0.01) + 0.55, 0.0, 1.0)
    rv_score = _clamp((0.95 - rv_ratio) / 0.55, 0.0, 1.0)
    breakout_score = _clamp(breakout_strength / 1.25, 0.0, 1.0)
    volume_score = _clamp((volume_ratio - 1.0) / 0.8, 0.0, 1.0)
    momentum_score = _clamp(momentum_in_atr / 1.5, 0.0, 1.0)
    raw = (
        0.24 * compression_score
        + 0.16 * rv_score
        + 0.28 * breakout_score
        + 0.14 * volume_score
        + 0.10 * momentum_score
        + 0.08 * max(0.0, alignment)
    )
    signed_score = raw * direction_sign
    threshold = _f(cfg.get("squeeze_min_score"), 0.58)
    accepted = compressed and breakout and confirmed and raw >= threshold
    return {
        "accepted": accepted,
        "reason": "LOW_IV_SQUEEZE_READY" if accepted else "LOW_IV_SQUEEZE_NOT_READY",
        "strategy": "LOW_IV_SQUEEZE",
        "score": float(signed_score),
        "direction": direction,
        "spot_price": float(price),
        "atr_pct": float(recent_atr / price),
        "realized_volatility": float(max(short_rv, medium_rv * 0.65)),
        "signal_bar_ts": int(_f(fast_rows[-1][0])),
        "requires_low_iv": True,
        "components": {
            "compression_ratio": compression_ratio,
            "rv_ratio": rv_ratio,
            "breakout_strength": breakout_strength,
            "volume_ratio": volume_ratio,
            "momentum_in_atr": momentum_in_atr,
            "slow_alignment": alignment,
        },
    }


def choose_underlying_signal(fast_rows, slow_rows, cfg=None):
    """Return the strongest accepted Trend/Squeeze setup for one underlying."""
    cfg = cfg or {}
    trend = evaluate_underlying_trend(fast_rows, slow_rows, cfg)
    squeeze = evaluate_low_iv_squeeze(fast_rows, slow_rows, cfg)
    accepted = [row for row in (trend, squeeze) if row.get("accepted")]
    if accepted:
        accepted.sort(key=lambda row: abs(_f(row.get("score"))), reverse=True)
        result = dict(accepted[0])
        result["alternate_signal"] = accepted[1].get("strategy") if len(accepted) > 1 else ""
        return result
    result = dict(trend if abs(_f(trend.get("score"))) >= abs(_f(squeeze.get("score"))) else squeeze)
    result["accepted"] = False
    result["reason"] = "NO_ADAPTIVE_OPTION_SIGNAL"
    result["trend_score"] = _f(trend.get("score"))
    result["squeeze_score"] = _f(squeeze.get("score"))
    return result


def derive_dynamic_contract_targets(signal, cfg=None):
    """Map signal type/strength to a DTE and Delta target inside hard bounds."""
    cfg = cfg or {}
    strength = abs(_f((signal or {}).get("score")))
    strategy = str((signal or {}).get("strategy") or "ADAPTIVE_TREND").upper()
    components = (signal or {}).get("components") or {}
    strong = _f(cfg.get("strong_signal"), 0.68)

    if strategy == "LOW_IV_SQUEEZE":
        target_dte = 3.5 if strength >= strong else 5.0
        target_delta = 0.42 if strength >= strong else 0.46
    else:
        persistent = (
            abs(_f(components.get("slow_timeframe"))) >= 0.45
            and abs(_f(components.get("medium"))) >= 0.40
            and abs(_f(components.get("breakout"))) < 0.55
        )
        if strength >= max(0.78, strong + 0.08):
            target_dte, target_delta = 5.0, 0.43
        elif persistent:
            target_dte, target_delta = 14.0, 0.50
        elif strength >= strong:
            target_dte, target_delta = 7.0, 0.45
        elif strength >= 0.56:
            target_dte, target_delta = 9.0, 0.47
        else:
            target_dte, target_delta = 12.0, 0.50

    min_dte = _f(cfg.get("min_dte_days"), 2.0)
    max_dte = _f(cfg.get("max_dte_days"), 21.0)
    target_dte = min(max_dte, max(min_dte, target_dte))
    preferred_min = _f(cfg.get("preferred_delta_min"), 0.35)
    preferred_max = _f(cfg.get("preferred_delta_max"), 0.55)
    target_delta = min(preferred_max, max(preferred_min, target_delta))
    return {
        "target_dte_days": target_dte,
        "target_delta": target_delta,
        "preferred_delta_min": preferred_min,
        "preferred_delta_max": preferred_max,
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


def shortlist_option_contracts_with_diagnostics(exchange_info, underlying, direction, spot_price, signal=None, cfg=None, now_ms=None):
    cfg = cfg or {}
    signal = signal or {}
    now_ms = int(now_ms or time.time() * 1000)
    direction = str(direction or "").upper()
    spot = _f(spot_price)
    targets = derive_dynamic_contract_targets(signal, cfg)
    diagnostics = {"DTE": 0, "OTM": 0, "SYMBOL": 0}
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
        diagnostics["SYMBOL"] += 1
        expiry = int(_f(contract.get("expiryDate")))
        dte = (expiry - now_ms) / 86_400_000.0
        if dte < _f(cfg.get("min_dte_days"), 2.0) or dte > _f(cfg.get("max_dte_days"), 21.0):
            diagnostics["DTE"] += 1
            continue
        strike = _f(contract.get("strikePrice"))
        if spot <= 0 or strike <= 0:
            diagnostics["OTM"] += 1
            continue
        otm = (strike / spot - 1.0) if direction == "CALL" else (spot / strike - 1.0)
        if otm < -0.08 or otm > 0.18:
            diagnostics["OTM"] += 1
            continue
        min_qty, step = _lot_filter(contract)
        row = dict(contract)
        row.update({
            "dte_days": dte,
            "otm_pct": otm,
            "min_qty": min_qty,
            "step_size": step,
            "tick_size": _price_filter(contract),
            **targets,
        })
        target_dte = targets["target_dte_days"]
        target_otm = _f(cfg.get("target_otm_pct"), 0.02)
        row["shortlist_distance"] = abs(dte - target_dte) / max(target_dte, 1.0) + abs(otm - target_otm) * 6.0
        rows.append(row)
    rows.sort(key=lambda item: (item["shortlist_distance"], item["dte_days"]))
    return rows[: int(cfg.get("max_candidates_per_underlying", 10) or 10)], diagnostics


def shortlist_option_contracts(exchange_info, underlying, direction, spot_price, cfg=None, now_ms=None, signal=None):
    rows, _ = shortlist_option_contracts_with_diagnostics(
        exchange_info, underlying, direction, spot_price, signal=signal, cfg=cfg, now_ms=now_ms
    )
    return rows


def _first_row(payload):
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


def _greek(mark, name):
    for key in (name, name.lower(), name.upper()):
        if key in mark:
            return _f(mark.get(key))
    return 0.0


def score_option_contract(contract, mark_payload, ticker_payload, depth_payload, signal, cfg=None, *, skew_mark_payload=None):
    """Apply hard safety gates, then rank survivable contracts with soft penalties."""
    cfg = cfg or {}
    mark = _first_row(mark_payload)
    ticker = _first_row(ticker_payload)
    peer_mark = _first_row(skew_mark_payload)
    bids = list((depth_payload or {}).get("bids") or [])
    asks = list((depth_payload or {}).get("asks") or [])
    bid = _f(bids[0][0]) if bids and len(bids[0]) >= 2 else _f(ticker.get("bidPrice"))
    ask = _f(asks[0][0]) if asks and len(asks[0]) >= 2 else _f(ticker.get("askPrice"))
    if bid <= 0 or ask <= 0 or ask <= bid:
        return {"accepted": False, "reason": "OPTION_ORDERBOOK_NOT_TRADEABLE"}
    mid = (bid + ask) / 2.0
    spread = (ask - bid) / mid if mid > 0 else 1.0
    max_spread = _f(cfg.get("max_spread_pct"), 0.18)
    if spread > max_spread:
        return {"accepted": False, "reason": "OPTION_SPREAD_TOO_WIDE", "spread_pct": spread}

    delta = abs(_f(mark.get("delta")))
    min_delta = _f(cfg.get("min_abs_delta"), 0.20)
    max_delta = _f(cfg.get("max_abs_delta"), 0.70)
    if delta < min_delta or delta > max_delta:
        return {"accepted": False, "reason": "OPTION_DELTA_OUTSIDE_TARGET", "delta": delta}

    quote_volume = _f(ticker.get("amount"))
    min_volume = _f(cfg.get("min_quote_volume_usdt"), 50.0)
    if quote_volume < min_volume:
        return {"accepted": False, "reason": "OPTION_VOLUME_TOO_LOW", "quote_volume": quote_volume}

    iv = _f(mark.get("markIV"))
    if iv <= 0:
        return {"accepted": False, "reason": "OPTION_IV_UNAVAILABLE"}
    realized = max(0.01, _f(signal.get("realized_volatility")))
    iv_ratio = iv / realized
    strategy = str(signal.get("strategy") or "ADAPTIVE_TREND").upper()
    hard_iv_ratio = _f(cfg.get("hard_max_iv_to_realized"), 2.40)
    if strategy == "LOW_IV_SQUEEZE":
        hard_iv_ratio = min(hard_iv_ratio, _f(cfg.get("squeeze_max_iv_to_realized"), 1.35))
    if iv_ratio > hard_iv_ratio:
        return {"accepted": False, "reason": "OPTION_IV_TOO_EXPENSIVE", "iv_to_realized": iv_ratio}

    depth_qty = min(_f(bids[0][1]), _f(asks[0][1])) if bids and asks else 0.0
    if depth_qty <= 0:
        return {"accepted": False, "reason": "OPTION_DEPTH_EMPTY"}

    targets = derive_dynamic_contract_targets(signal, cfg)
    target_delta = _f(contract.get("target_delta"), targets["target_delta"])
    target_dte = _f(contract.get("target_dte_days"), targets["target_dte_days"])
    dte = _f(contract.get("dte_days"), target_dte)
    signal_strength = abs(_f(signal.get("score")))

    soft_iv = _f(cfg.get("max_iv_to_realized"), 1.60)
    spread_score = 1.0 - min(spread / max(max_spread, 0.01), 1.0)
    delta_score = 1.0 - min(abs(delta - target_delta) / 0.30, 1.0)
    dte_score = 1.0 - min(abs(dte - target_dte) / max(target_dte, 2.0), 1.0)
    if iv_ratio <= 1.0:
        iv_score = 1.0
    elif iv_ratio <= soft_iv:
        iv_score = 1.0 - 0.35 * ((iv_ratio - 1.0) / max(soft_iv - 1.0, 0.01))
    else:
        iv_score = max(0.0, 0.65 * (hard_iv_ratio - iv_ratio) / max(hard_iv_ratio - soft_iv, 0.01))

    peer_iv = _f(peer_mark.get("markIV"))
    skew_ratio = 0.0
    skew_score = 0.5
    if peer_iv > 0:
        skew_ratio = (iv - peer_iv) / max(peer_iv, 0.01)
        skew_score = _clamp(0.5 - skew_ratio / 0.8, 0.0, 1.0)

    open_interest = max(_f(ticker.get("openInterest")), _f(mark.get("openInterest")), _f(contract.get("openInterest")))
    liquidity_score = min(1.0, math.log1p(max(0.0, quote_volume)) / math.log1p(max(1000.0, min_volume * 20.0)))
    depth_score = min(depth_qty / max(_f(contract.get("min_qty"), 0.01) * 50.0, 1.0), 1.0)
    if open_interest > 0:
        liquidity_score = min(1.0, liquidity_score * 0.75 + min(math.log1p(open_interest) / 8.0, 1.0) * 0.25)

    gamma = _greek(mark, "gamma")
    theta = _greek(mark, "theta")
    vega = _greek(mark, "vega")
    theta_burden = abs(theta) / max(mid, 1e-9) if theta else 0.0
    greek_score = 1.0 - min(theta_burden / 0.25, 1.0)
    if gamma > 0 and strategy == "LOW_IV_SQUEEZE":
        greek_score = min(1.0, greek_score + 0.10)

    score = (
        signal_strength * 25.0
        + spread_score * 15.0
        + delta_score * 15.0
        + dte_score * 12.0
        + iv_score * 14.0
        + skew_score * 8.0
        + liquidity_score * 6.0
        + depth_score * 3.0
        + greek_score * 2.0
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
        "open_interest": open_interest,
        "skew_peer_iv": peer_iv,
        "skew_ratio": skew_ratio,
        "target_delta": target_delta,
        "target_dte_days": target_dte,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "theta_burden": theta_burden,
    }


def find_skew_peer(exchange_info, contract):
    """Find the opposite-side contract with the closest strike at the same expiry."""
    side = str((contract or {}).get("side") or "").upper()
    opposite = "PUT" if side == "CALL" else "CALL"
    underlying = str((contract or {}).get("underlying") or "").upper()
    expiry = int(_f((contract or {}).get("expiryDate")))
    strike = _f((contract or {}).get("strikePrice"))
    peers = []
    for row in (exchange_info or {}).get("optionSymbols") or []:
        if str(row.get("underlying") or "").upper() != underlying:
            continue
        if str(row.get("side") or "").upper() != opposite:
            continue
        if int(_f(row.get("expiryDate"))) != expiry:
            continue
        if str(row.get("status") or "").upper() != "TRADING":
            continue
        peers.append(row)
    if not peers:
        return None
    peers.sort(key=lambda row: abs(_f(row.get("strikePrice")) - strike))
    return peers[0]


__all__ = (
    "choose_underlying_signal",
    "derive_dynamic_contract_targets",
    "evaluate_low_iv_squeeze",
    "evaluate_underlying_trend",
    "find_skew_peer",
    "score_option_contract",
    "shortlist_option_contracts",
    "shortlist_option_contracts_with_diagnostics",
)

"""Probability and edge helpers for binary prediction contracts."""

from __future__ import annotations

from math import erf, log, sqrt


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, _finite_float(value, low)))


def _norm_cdf(value):
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def estimate_crypto_up_probability(
    *,
    open_price,
    current_price,
    minutes_remaining,
    realized_vol_pct,
    drift_pct=0.0,
):
    open_price = _finite_float(open_price)
    current_price = _finite_float(current_price)
    minutes_remaining = max(0.0, _finite_float(minutes_remaining))
    realized_vol_pct = max(0.0, _finite_float(realized_vol_pct))
    drift_pct = _finite_float(drift_pct)
    if open_price <= 0 or current_price <= 0:
        return 0.5
    distance = log(current_price / open_price)
    if minutes_remaining <= 0:
        return 1.0 if current_price >= open_price else 0.0
    sigma = max(realized_vol_pct / 100.0, 0.0001) * sqrt(max(minutes_remaining, 1.0) / 60.0)
    drift = drift_pct / 100.0 * minutes_remaining / 60.0
    return clamp(_norm_cdf((distance + drift) / sigma))


def evaluate_prediction_edge(
    *,
    fair_probability,
    market_price,
    fee_rate_bps=200,
    spread_decimal=0.0,
    safety_margin=0.03,
):
    fair = clamp(fair_probability)
    price = clamp(market_price)
    fee = max(0.0, _finite_float(fee_rate_bps)) / 10000.0 * min(price, 1.0 - price)
    spread_cost = max(0.0, _finite_float(spread_decimal)) / 2.0
    margin = max(0.0, _finite_float(safety_margin))
    required_probability = price + fee + spread_cost + margin
    edge = fair - required_probability
    return {
        "accepted": edge > 0.0,
        "fair_probability": fair,
        "market_price": price,
        "fee_cost_probability": fee,
        "spread_cost_probability": spread_cost,
        "safety_margin": margin,
        "required_probability": required_probability,
        "edge": edge,
        "reject_code": None if edge > 0.0 else "REJECTED_PREDICTION_EDGE_LOW",
    }

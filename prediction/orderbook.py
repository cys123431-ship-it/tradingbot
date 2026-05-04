"""Orderbook analytics for binary YES-priced markets."""

from __future__ import annotations


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else default


def _level_tuple(level):
    if isinstance(level, dict):
        price = _finite_float(level.get("price") or level.get("p") or level.get("0"))
        size = _finite_float(level.get("size") or level.get("shares") or level.get("quantity") or level.get("1"))
        return price, size
    if isinstance(level, (list, tuple)):
        price = _finite_float(level[0] if len(level) > 0 else 0.0)
        size = _finite_float(level[1] if len(level) > 1 else 0.0)
        return price, size
    return 0.0, 0.0


def _levels(raw):
    parsed = [_level_tuple(item) for item in (raw or [])]
    return [(p, s) for p, s in parsed if 0.0 < p < 1.0 and s > 0.0]


def _walk_cost(levels, spend_usdt):
    remaining = max(0.0, _finite_float(spend_usdt))
    cost = 0.0
    shares = 0.0
    for price, size in sorted(levels, key=lambda item: item[0]):
        level_cost = price * size
        take_cost = min(remaining, level_cost)
        if take_cost <= 0:
            break
        cost += take_cost
        shares += take_cost / price
        remaining -= take_cost
    avg_price = cost / shares if shares > 0 else None
    return {
        "filled_cost": cost,
        "filled_shares": shares,
        "avg_price": avg_price,
        "unfilled_cost": remaining,
    }


def analyze_orderbook(payload, spend_usdt=1.0):
    data = (payload or {}).get("data", payload or {})
    bids = sorted(_levels(data.get("bids")), key=lambda item: item[0], reverse=True)
    asks = sorted(_levels(data.get("asks")), key=lambda item: item[0])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
    mid = ((best_bid + best_ask) / 2.0) if spread is not None else (best_ask or best_bid)
    walk = _walk_cost(asks, spend_usdt)
    impact = None
    if walk["avg_price"] is not None and best_ask:
        impact = max(0.0, walk["avg_price"] - best_ask)
    return {
        "best_yes_bid": best_bid,
        "best_yes_ask": best_ask,
        "yes_mid": mid,
        "yes_spread": spread,
        "yes_spread_pct": (spread / mid * 100.0) if spread is not None and mid else None,
        "buy_yes_avg_price": walk["avg_price"],
        "buy_yes_price_impact": impact,
        "buy_yes_unfilled_usdt": walk["unfilled_cost"],
        "bid_depth_shares": sum(size for _, size in bids),
        "ask_depth_shares": sum(size for _, size in asks),
        "accepted": best_ask is not None,
        "reject_code": None if best_ask is not None else "REJECTED_PREDICTION_ORDERBOOK_EMPTY",
    }

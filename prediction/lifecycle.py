"""Paper position lifecycle rules for Prediction Micro Auto."""

from __future__ import annotations

from datetime import datetime, timezone

from .micro_risk import normalize_prediction_micro_config
from .models import extract_market_resolution


def _finite_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _parse_dt(value):
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def best_paper_exit_price(orderbook, side="YES"):
    """Return the price used for a paper close.

    The first live phase only opens YES positions. NO support is included for
    future paper tests but does not create any live order path.
    """
    orderbook = orderbook or {}
    side = str(side or "YES").upper()
    if side == "YES":
        for key in ("best_yes_bid", "yes_mid", "buy_yes_avg_price", "best_yes_ask"):
            value = _finite_float(orderbook.get(key))
            if value is not None and 0.0 <= value <= 1.0:
                return value
    for key in ("best_yes_ask", "yes_mid", "best_yes_bid"):
        value = _finite_float(orderbook.get(key))
        if value is not None and 0.0 <= value <= 1.0:
            return 1.0 - value
    return None


def evaluate_paper_position_exit(position, *, raw_market=None, orderbook=None, edge_result=None, cfg=None, now=None):
    cfg = normalize_prediction_micro_config(cfg)
    position = position or {}
    resolution = extract_market_resolution(raw_market or {})
    side = str(position.get("side") or "YES").upper()
    if resolution and resolution.get("resolved"):
        won = str(resolution.get("winning_side") or "").upper() == side
        return {
            "action": "settle",
            "reason": "PAPER_SETTLED_BY_MARKET_RESOLUTION",
            "outcome_won": won,
            "closing_price": 1.0 if won else 0.0,
        }

    exit_price = best_paper_exit_price(orderbook, side=side)
    if exit_price is None:
        return {"action": "hold", "reason": "PAPER_HOLD_NO_EXIT_PRICE"}

    entry_price = _finite_float(position.get("entry_price"), 0.0) or 0.0
    if entry_price > 0:
        if exit_price <= max(0.0, entry_price - cfg["paper_stop_loss_probability"]):
            return {
                "action": "close",
                "reason": "PAPER_CLOSE_PROBABILITY_STOP",
                "exit_price": exit_price,
            }
        if exit_price >= min(1.0, entry_price + cfg["paper_take_profit_probability"]):
            return {
                "action": "close",
                "reason": "PAPER_CLOSE_PROBABILITY_TAKE_PROFIT",
                "exit_price": exit_price,
            }

    edge = _finite_float((edge_result or {}).get("edge"))
    if edge is not None and edge <= cfg["paper_exit_edge_floor"]:
        return {
            "action": "close",
            "reason": "PAPER_CLOSE_EDGE_GONE",
            "exit_price": exit_price,
        }

    opened_at = _parse_dt(position.get("opened_at"))
    now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    if opened_at:
        hold_hours = (now_dt - opened_at).total_seconds() / 3600.0
        if hold_hours >= cfg["paper_max_hold_hours"]:
            return {
                "action": "close",
                "reason": "PAPER_CLOSE_MAX_HOLD",
                "exit_price": exit_price,
            }
    return {"action": "hold", "reason": "PAPER_HOLD_RULES_OK", "exit_price": exit_price}

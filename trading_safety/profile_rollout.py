"""Durable activation boundary for entry-profile rollouts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .binance_algo_gateway import normalize_futures_market_id


TRADFI_PROFILE_ROLLOUT_VERSION = "tradfi_pattern_profile_v2_session_anchor"
TRADFI_PROFILE_ROLLOUT_STATE_KEY = "tradfi_pattern_profile_rollout"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _symbol(value: Any) -> str:
    try:
        return normalize_futures_market_id(value)
    except Exception:
        return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _item_symbol(item: Any) -> str:
    data = _payload(item)
    info = _payload(data.get("info"))
    return _symbol(data.get("symbol") or info.get("symbol"))


def _position_qty(item: Any) -> float:
    data = _payload(item)
    info = _payload(data.get("info"))
    try:
        return abs(float(data.get("contracts") or info.get("positionAmt") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_protection_order(item: Any) -> bool:
    data = _payload(item)
    info = _payload(data.get("info"))
    order_type = str(
        data.get("type")
        or data.get("orderType")
        or info.get("type")
        or info.get("orderType")
        or ""
    ).upper()
    return bool(
        _truthy(data.get("reduceOnly"))
        or _truthy(info.get("reduceOnly"))
        or _truthy(data.get("closePosition"))
        or _truthy(info.get("closePosition"))
        or "STOP" in order_type
        or "TAKE_PROFIT" in order_type
    )


def _complete_snapshot(result: Any) -> bool:
    return bool(
        getattr(result, "snapshot_complete", False)
        and getattr(result, "positions_ok", False)
        and getattr(result, "regular_orders_ok", False)
        and getattr(result, "algo_orders_ok", False)
    )


def get_tradfi_profile_rollout_state(store: Any) -> dict[str, Any]:
    if store is None or not hasattr(store, "get_runtime_state"):
        return {
            "version": TRADFI_PROFILE_ROLLOUT_VERSION,
            "state": "unavailable",
            "active": False,
        }
    state = store.get_runtime_state(TRADFI_PROFILE_ROLLOUT_STATE_KEY)
    if not isinstance(state, dict):
        return {
            "version": TRADFI_PROFILE_ROLLOUT_VERSION,
            "state": "uninitialized",
            "active": False,
        }
    current = dict(state)
    current["active"] = bool(
        current.get("version") == TRADFI_PROFILE_ROLLOUT_VERSION
        and current.get("state") == "active"
    )
    return current


def update_tradfi_profile_rollout(store: Any, result: Any) -> dict[str, Any]:
    """Initialize or advance the rollout using one complete exchange snapshot.

    A deployment that observes an open position is marked pending.  It becomes
    active only after a later complete snapshot is flat and the orders attached
    to the deployment-time position symbols are also gone.
    """

    if store is None or not hasattr(store, "get_runtime_state"):
        return get_tradfi_profile_rollout_state(store)
    existing = store.get_runtime_state(TRADFI_PROFILE_ROLLOUT_STATE_KEY)
    existing_missing = not isinstance(existing, dict)
    version_changed = bool(
        isinstance(existing, dict)
        and existing.get("version") != TRADFI_PROFILE_ROLLOUT_VERSION
    )
    if existing_missing or version_changed:
        if not _complete_snapshot(result):
            return {
                "version": TRADFI_PROFILE_ROLLOUT_VERSION,
                "state": "awaiting_complete_snapshot",
                "active": False,
            }
        position_symbols = {
            symbol
            for item in list(getattr(result, "positions", None) or [])
            if _position_qty(item) > 0 and (symbol := _item_symbol(item))
        }
        protection_symbols = {
            symbol
            for item in list(getattr(result, "open_orders", None) or [])
            if _is_protection_order(item) and (symbol := _item_symbol(item))
        }
        pending_symbols = sorted(position_symbols | protection_symbols)
        initial = {
            "version": TRADFI_PROFILE_ROLLOUT_VERSION,
            "state": "pending_existing_position" if pending_symbols else "active",
            "active": not bool(pending_symbols),
            "pending_symbols": pending_symbols,
            "initialized_at": _now_iso(),
            "activated_at": None if pending_symbols else _now_iso(),
            "reason": (
                "deployment observed open position/protection; preserve legacy profile until clear"
                if pending_symbols
                else "deployment exchange snapshot was flat"
            ),
        }
        create = getattr(store, "create_runtime_state_if_absent", None)
        if version_changed:
            # Versioned entry profiles share one durable state key. An
            # insert-if-absent call cannot replace the previous version and
            # would leave every later rollout permanently inactive.
            store.set_runtime_state(TRADFI_PROFILE_ROLLOUT_STATE_KEY, initial)
            existing = initial
        elif callable(create):
            inserted = bool(create(TRADFI_PROFILE_ROLLOUT_STATE_KEY, initial))
            if not inserted:
                existing = store.get_runtime_state(TRADFI_PROFILE_ROLLOUT_STATE_KEY)
            else:
                existing = initial
        else:
            store.set_runtime_state(TRADFI_PROFILE_ROLLOUT_STATE_KEY, initial)
            existing = initial

    state = dict(existing or {})
    if state.get("version") != TRADFI_PROFILE_ROLLOUT_VERSION:
        return get_tradfi_profile_rollout_state(store)
    if state.get("state") == "active":
        state["active"] = True
        return state
    if not _complete_snapshot(result):
        state["active"] = False
        state["reason"] = "waiting for complete position and order snapshot"
        return state

    pending_symbols = {
        _symbol(value) for value in state.get("pending_symbols", ()) if _symbol(value)
    }
    open_position_symbols = {
        symbol
        for item in list(getattr(result, "positions", None) or [])
        if _position_qty(item) > 0 and (symbol := _item_symbol(item))
    }
    open_order_symbols = {
        symbol
        for item in list(getattr(result, "open_orders", None) or [])
        if (symbol := _item_symbol(item))
    }
    remaining_positions = sorted(open_position_symbols & pending_symbols)
    remaining_orders = sorted(open_order_symbols & pending_symbols)
    if remaining_positions or remaining_orders:
        state.update(
            {
                "active": False,
                "state": "pending_existing_position",
                "remaining_positions": remaining_positions,
                "remaining_orders": remaining_orders,
                "last_checked_at": _now_iso(),
                "reason": "waiting for deployment-time position and protection orders to clear",
            }
        )
        store.set_runtime_state(TRADFI_PROFILE_ROLLOUT_STATE_KEY, state)
        return state

    state.update(
        {
            "active": True,
            "state": "active",
            "activated_at": _now_iso(),
            "last_checked_at": _now_iso(),
            "remaining_positions": [],
            "remaining_orders": [],
            "reason": "exchange confirmed flat and deployment-time orders cleared",
        }
    )
    store.set_runtime_state(TRADFI_PROFILE_ROLLOUT_STATE_KEY, state)
    return state


__all__ = (
    "TRADFI_PROFILE_ROLLOUT_VERSION",
    "TRADFI_PROFILE_ROLLOUT_STATE_KEY",
    "get_tradfi_profile_rollout_state",
    "update_tradfi_profile_rollout",
)

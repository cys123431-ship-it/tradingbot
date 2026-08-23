"""Fail-closed post-fill protection for Adaptive Trend pyramid adds."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _position_qty(engine: Any, pos: dict[str, Any] | None) -> float:
    if not isinstance(pos, dict):
        return 0.0
    try:
        resolver = getattr(engine, "_position_signed_contracts", None)
        raw = resolver(pos) if callable(resolver) else pos.get("contracts", 0.0)
        return abs(float(raw or pos.get("contracts", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _required_stop(
    side: str,
    average_entry: float,
    cost_buffer_percent: float,
    previous_stop: float | None,
    state_stop: float | None,
) -> float:
    multiplier = max(0.0, min(0.50, float(cost_buffer_percent))) / 100.0
    fee_break_even = average_entry * (
        1.0 + multiplier if side == "long" else 1.0 - multiplier
    )
    candidates = [fee_break_even]
    for value in (previous_stop, state_stop):
        stop = _as_float(value)
        if stop is not None and stop > 0:
            candidates.append(stop)
    return max(candidates) if side == "long" else min(candidates)


def _stop_satisfies(side: str, actual: float, required: float) -> bool:
    tolerance = max(abs(required) * 1e-8, 1e-10)
    if side == "long":
        return actual + tolerance >= required
    return actual - tolerance <= required


def _set_entry_lock(engine: Any, reason: str | None) -> None:
    setter = getattr(engine, "_set_crypto_entry_lock", None)
    if callable(setter):
        setter(reason)


def _mark_latest_position_add_protected(
    engine: Any,
    symbol: str,
    replacement: Any,
) -> None:
    store = getattr(engine, "trading_state_store", None)
    if store is None:
        return
    active_for_symbol = getattr(store, "active_for_symbol", None)
    transition = getattr(store, "transition", None)
    if not callable(active_for_symbol) or not callable(transition):
        return
    try:
        candidates = [
            record
            for record in (active_for_symbol(symbol) or [])
            if str(getattr(record, "order_intent", "") or "") == "POSITION_ADD"
            and str(getattr(record, "strategy", "") or "").upper()
            == "ADAPTIVE_TREND_PYRAMID"
            and str(getattr(record, "order_state", "") or "") != "PROTECTED"
        ]
        if not candidates:
            return
        latest = max(
            candidates,
            key=lambda record: str(getattr(record, "created_at", "") or ""),
        )
        changes: dict[str, Any] = {}
        order_id_resolver = getattr(engine, "_protection_order_id", None)
        if callable(order_id_resolver):
            stop_order_id = order_id_resolver(replacement)
            if stop_order_id:
                changes["stop_order_id"] = stop_order_id
        transition(latest.client_order_id, "PROTECTED", **changes)
    except Exception:
        logger = getattr(engine, "logger", None)
        if logger is not None:
            logger.exception(
                "Failed to mark repaired pyramid add protected: %s",
                symbol,
            )


async def _emergency_close(engine: Any, symbol: str, reason: str) -> dict[str, Any]:
    closer = getattr(engine, "_emergency_close_position_without_stop_loss", None)
    if not callable(closer):
        _set_entry_lock(engine, f"FILLED_UNPROTECTED:{symbol}")
        return {
            "status": "FILLED_UNPROTECTED",
            "reason": reason,
            "closed": False,
        }
    close_status = await closer(
        symbol,
        reason=reason,
        max_attempts=5,
    )
    if bool((close_status or {}).get("closed")):
        return {
            "status": "EMERGENCY_CLOSED",
            "reason": reason,
            "closed": True,
            "close_status": close_status,
        }
    return {
        "status": str((close_status or {}).get("status") or "CRITICAL_PAUSED"),
        "reason": reason,
        "closed": False,
        "close_status": close_status,
    }


async def enforce_adaptive_pyramid_stop_postcondition(
    engine: Any,
    symbol: str,
    *,
    before_qty: float,
    before_stop: float | None,
    before_add_count: int,
    result: Any,
    cfg: dict[str, Any] | None = None,
) -> Any:
    """Verify a filled winner add is protected by the full-quantity profit-lock SL."""

    if not isinstance(result, dict):
        return result
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or "")
    geometry_failed_after_fill = (
        status == "BLOCKED"
        and "adaptive trend average entry/sl geometry invalid" in reason.lower()
    )
    if status != "ADDED" and not geometry_failed_after_fill:
        return result

    fetcher = getattr(engine, "_fetch_server_position_checked", None)
    if not callable(fetcher):
        _set_entry_lock(engine, f"FILLED_PROTECTION_UNVERIFIED:{symbol}")
        return {
            **result,
            "status": "FILLED_PROTECTION_UNVERIFIED",
            "reason": "adaptive trend pyramid post-fill position verification unavailable",
        }

    fetch_ok, live_pos = await fetcher(symbol)
    if not fetch_ok:
        _set_entry_lock(engine, f"FILLED_PROTECTION_UNVERIFIED:{symbol}")
        return {
            **result,
            "status": "FILLED_PROTECTION_UNVERIFIED",
            "reason": "adaptive trend pyramid post-fill position fetch failed",
        }
    if not live_pos:
        return result

    live_qty = _position_qty(engine, live_pos)
    if live_qty <= float(before_qty or 0.0) + 1e-12:
        return result

    side = str(live_pos.get("side") or "").lower()
    average_entry = _as_float(
        live_pos.get("entryPrice") or live_pos.get("entry_price")
    )
    if side not in {"long", "short"} or average_entry is None or average_entry <= 0:
        return await _emergency_close(
            engine,
            symbol,
            "adaptive trend pyramid post-fill side/average entry invalid",
        )

    state_getter = getattr(engine, "_get_utbreakout_trailing_state", None)
    state = state_getter(symbol) if callable(state_getter) else None
    if not isinstance(state, dict):
        state = {}

    aggressive = bool(state.get("small_account_aggressive_active", False))
    default_buffer = (
        state.get("small_account_aggressive_cost_buffer_percent", 0.20)
        if aggressive
        else (cfg or {}).get("adaptive_trend_breakeven_cost_buffer_percent", 0.12)
    )
    try:
        cost_buffer_percent = max(0.0, min(0.50, float(default_buffer or 0.0)))
    except (TypeError, ValueError):
        cost_buffer_percent = 0.20 if aggressive else 0.12

    required_stop = _required_stop(
        side,
        average_entry,
        cost_buffer_percent,
        _as_float(before_stop),
        _as_float(state.get("last_stop_price") or state.get("stop_loss")),
    )
    mark_price = _as_float(
        live_pos.get("markPrice")
        or live_pos.get("mark_price")
        or live_pos.get("lastPrice")
        or live_pos.get("last")
    )
    target_is_live = bool(
        mark_price is None
        or (side == "long" and required_stop < mark_price)
        or (side == "short" and required_stop > mark_price)
    )
    if not target_is_live:
        return await _emergency_close(
            engine,
            symbol,
            "adaptive trend pyramid required profit-lock SL already crossed after add",
        )

    auditor = getattr(engine, "_audit_protection_orders", None)
    if not callable(auditor):
        _set_entry_lock(engine, f"FILLED_PROTECTION_UNVERIFIED:{symbol}")
        return {
            **result,
            "status": "FILLED_PROTECTION_UNVERIFIED",
            "reason": "adaptive trend pyramid SL audit unavailable",
        }

    pre_audit = await auditor(
        symbol,
        pos=live_pos,
        expected_sl=True,
        alert=True,
    )
    if not bool((pre_audit or {}).get("fetch_ok")):
        _set_entry_lock(engine, f"FILLED_PROTECTION_UNVERIFIED:{symbol}")
        return {
            **result,
            "status": "FILLED_PROTECTION_UNVERIFIED",
            "reason": "adaptive trend pyramid SL snapshot unavailable",
            "audit": pre_audit,
        }

    if bool((pre_audit or {}).get("sl_qty_mismatch")):
        cancel_by_kind = getattr(engine, "_cancel_protection_orders_by_kind", None)
        if callable(cancel_by_kind):
            await cancel_by_kind(
                symbol,
                {"sl"},
                reason="Adaptive Trend pyramid full-quantity SL repair",
            )

    replacer = getattr(engine, "_replace_stop_loss_order", None)
    if not callable(replacer):
        return await _emergency_close(
            engine,
            symbol,
            "adaptive trend pyramid SL replacement unavailable",
        )
    replacement = await replacer(
        symbol,
        live_pos,
        required_stop,
        reason="Adaptive Trend pyramid full-quantity profit-lock postcondition",
    )
    if not replacement:
        return await _emergency_close(
            engine,
            symbol,
            "adaptive trend pyramid required full-quantity SL replacement failed",
        )

    post_audit = await auditor(
        symbol,
        pos=live_pos,
        expected_sl=True,
        alert=True,
    )
    stop_reader = getattr(engine, "_current_stop_loss_price", None)
    actual_stop = await stop_reader(symbol, state) if callable(stop_reader) else None
    actual_stop = _as_float(actual_stop)
    verified = bool(
        (post_audit or {}).get("fetch_ok")
        and (post_audit or {}).get("sl_present")
        and not (post_audit or {}).get("sl_qty_mismatch")
        and actual_stop is not None
        and _stop_satisfies(side, actual_stop, required_stop)
    )
    if not verified:
        return await _emergency_close(
            engine,
            symbol,
            "adaptive trend pyramid SL price/quantity postcondition failed",
        )

    state.update(
        {
            "active": True,
            "breakeven_armed": True,
            "entry_price": average_entry,
            "initial_qty": live_qty,
            "risk_distance": abs(average_entry - actual_stop),
            "stop_loss": actual_stop,
            "hard_stop_loss": actual_stop,
            "last_stop_price": actual_stop,
            "adaptive_trend_breakeven_cost_buffer_percent": cost_buffer_percent,
            "adaptive_trend_pyramid_add_count": max(
                int(state.get("adaptive_trend_pyramid_add_count", 0) or 0),
                int(before_add_count or 0) + 1,
            ),
            "last_update_ts": datetime.now(timezone.utc).isoformat(),
        }
    )
    state_setter = getattr(engine, "_set_utbreakout_trailing_state", None)
    if callable(state_setter):
        state_setter(symbol, state)

    _mark_latest_position_add_protected(engine, symbol, replacement)
    current_lock = str(getattr(engine, "crypto_entry_lock_reason", "") or "")
    if current_lock.startswith("FILLED_"):
        _set_entry_lock(engine, None)

    output = dict(result)
    if geometry_failed_after_fill:
        output.update(
            {
                "status": "ADDED_PROTECTION_REPAIRED",
                "reason": "adaptive trend pyramid profit-lock SL repaired after filled add",
            }
        )
    output.update(
        {
            "post_add_stop_guard": "OK",
            "required_stop": required_stop,
            "verified_stop": actual_stop,
            "position_qty": live_qty,
        }
    )
    return output


__all__ = ("enforce_adaptive_pyramid_stop_postcondition",)

"""Exchange-verified post-fill SL guard for Adaptive Trend pyramid adds."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .pyramid_protection import (
    _as_float,
    _emergency_close,
    _mark_latest_position_add_protected,
    _position_qty,
    _required_stop,
    _set_entry_lock,
    _stop_satisfies,
)


def _qty_matches(engine: Any, expected: float, actual: float | None) -> bool:
    if actual is None:
        return False
    matcher = getattr(engine, "_qty_matches_plan", None)
    if callable(matcher):
        try:
            return bool(matcher(expected, actual))
        except Exception:
            pass
    tolerance = max(1e-9, abs(float(expected)) * 0.001)
    return abs(float(actual) - float(expected)) <= tolerance


def _stop_live(side: str, stop: float | None, mark: float | None) -> bool:
    if stop is None or stop <= 0:
        return False
    if mark is None or mark <= 0:
        return True
    return stop < mark if side == "long" else stop > mark


async def read_exchange_sl_snapshot(engine: Any, symbol: str, side: str) -> dict[str, Any]:
    """Read real open exchange SL orders instead of cached trailing-state prices."""
    collector = getattr(engine, "_collect_protection_orders_checked", None)
    classifier = getattr(engine, "_classify_protection_order", None)
    price_reader = getattr(engine, "_protection_trigger_price", None)
    qty_reader = getattr(engine, "_protection_order_amount", None)
    side_reader = getattr(engine, "_protection_order_side", None)
    if not all(callable(x) for x in (collector, classifier, price_reader, qty_reader)):
        return {"fetch_ok": False, "entries": []}
    try:
        fetch_ok, orders = await collector(symbol)
    except Exception:
        return {"fetch_ok": False, "entries": []}
    if not fetch_ok:
        return {"fetch_ok": False, "entries": []}

    close_side = "sell" if side == "long" else "buy"
    entries: list[dict[str, Any]] = []
    for order in orders or []:
        try:
            if classifier(order) != "sl":
                continue
        except Exception:
            continue
        if callable(side_reader):
            try:
                order_side = str(side_reader(order) or "").lower()
            except Exception:
                order_side = ""
            if order_side and order_side != close_side:
                continue
        try:
            stop = _as_float(price_reader(order))
        except Exception:
            stop = None
        if stop is None or stop <= 0:
            continue
        try:
            qty = _as_float(qty_reader(order))
        except Exception:
            qty = None
        entries.append({"order": order, "stop": stop, "qty": qty})
    return {"fetch_ok": True, "entries": entries}


def best_live_exchange_stop(snapshot: dict[str, Any], side: str, mark: float | None) -> float | None:
    stops = [
        float(item["stop"])
        for item in snapshot.get("entries") or []
        if _stop_live(side, _as_float(item.get("stop")), mark)
    ]
    if not stops:
        return None
    return max(stops) if side == "long" else min(stops)


def _verified_entry(
    engine: Any,
    snapshot: dict[str, Any],
    side: str,
    required_stop: float,
    expected_qty: float,
) -> dict[str, Any] | None:
    matches = []
    for item in snapshot.get("entries") or []:
        stop = _as_float(item.get("stop"))
        qty = _as_float(item.get("qty"))
        if stop is None or not _qty_matches(engine, expected_qty, qty):
            continue
        if _stop_satisfies(side, stop, required_stop):
            matches.append(item)
    if not matches:
        return None
    key = lambda item: float(item["stop"])
    return max(matches, key=key) if side == "long" else min(matches, key=key)


def _same_price_wrong_qty(
    engine: Any,
    snapshot: dict[str, Any],
    target_stop: float,
    expected_qty: float,
) -> bool:
    tolerance = max(abs(target_stop) * 1e-8, 1e-10)
    for item in snapshot.get("entries") or []:
        stop = _as_float(item.get("stop"))
        qty = _as_float(item.get("qty"))
        if stop is None or abs(stop - target_stop) > tolerance:
            continue
        if not _qty_matches(engine, expected_qty, qty):
            return True
    return False


async def _poll_verified(
    engine: Any,
    symbol: str,
    side: str,
    required_stop: float,
    expected_qty: float,
    attempts: int,
    delay_sec: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    last = {"fetch_ok": False, "entries": []}
    for attempt in range(max(1, attempts)):
        last = await read_exchange_sl_snapshot(engine, symbol, side)
        if last.get("fetch_ok"):
            verified = _verified_entry(engine, last, side, required_stop, expected_qty)
            if verified is not None:
                return verified, last
        if attempt < max(1, attempts) - 1 and delay_sec > 0:
            await asyncio.sleep(delay_sec)
    return None, last


async def _replace_then_verify(
    engine: Any,
    symbol: str,
    live_pos: dict[str, Any],
    side: str,
    target_stop: float,
    expected_qty: float,
    snapshot: dict[str, Any],
    attempts: int,
    delay_sec: float,
    reason: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    replacer = getattr(engine, "_replace_stop_loss_order", None)
    if not callable(replacer):
        return None, snapshot

    # Existing replacement logic can return an old same-price SL even when it
    # covers only the pre-add quantity. Cancel only that exact stale-qty case.
    if _same_price_wrong_qty(engine, snapshot, target_stop, expected_qty):
        cancel_by_kind = getattr(engine, "_cancel_protection_orders_by_kind", None)
        if callable(cancel_by_kind):
            await cancel_by_kind(
                symbol,
                {"sl"},
                reason=f"{reason}: stale pre-add SL quantity",
            )
            snapshot = await read_exchange_sl_snapshot(engine, symbol, side)

    try:
        await replacer(symbol, live_pos, target_stop, reason=reason)
    except Exception:
        # Binance may have accepted the conditional order even if the response
        # was lost. Poll the real exchange state before deciding to flatten.
        pass

    return await _poll_verified(
        engine,
        symbol,
        side,
        target_stop,
        expected_qty,
        attempts,
        delay_sec,
    )


async def enforce_adaptive_pyramid_live_sl_guard(
    engine: Any,
    symbol: str,
    *,
    before_qty: float,
    before_stop: float | None,
    before_add_count: int,
    result: Any,
    cfg: dict[str, Any] | None = None,
) -> Any:
    """Require a real exchange SL for the enlarged position before accepting the add."""
    if not isinstance(result, dict):
        return result
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or "")
    geometry_repair = (
        status == "BLOCKED"
        and "adaptive trend average entry/sl geometry invalid" in reason.lower()
    )
    if status != "ADDED" and not geometry_repair:
        return result

    fetcher = getattr(engine, "_fetch_server_position_checked", None)
    if not callable(fetcher):
        _set_entry_lock(engine, f"FILLED_PROTECTION_UNVERIFIED:{symbol}")
        return {**result, "status": "FILLED_PROTECTION_UNVERIFIED"}
    fetch_ok, live_pos = await fetcher(symbol)
    if not fetch_ok:
        _set_entry_lock(engine, f"FILLED_PROTECTION_UNVERIFIED:{symbol}")
        return {**result, "status": "FILLED_PROTECTION_UNVERIFIED"}
    if not live_pos:
        return result

    live_qty = _position_qty(engine, live_pos)
    if live_qty <= float(before_qty or 0.0) + 1e-12:
        return result
    side = str(live_pos.get("side") or "").lower()
    avg_entry = _as_float(live_pos.get("entryPrice") or live_pos.get("entry_price"))
    mark = _as_float(
        live_pos.get("markPrice")
        or live_pos.get("mark_price")
        or live_pos.get("lastPrice")
        or live_pos.get("last")
    )
    if side not in {"long", "short"} or avg_entry is None or avg_entry <= 0:
        return await _emergency_close(engine, symbol, "adaptive trend pyramid invalid post-fill position")

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
        cost_buffer = max(0.0, min(0.50, float(default_buffer or 0.0)))
    except (TypeError, ValueError):
        cost_buffer = 0.20 if aggressive else 0.12
    try:
        min_gap = max(
            0.10,
            min(
                1.00,
                float(
                    state.get("adaptive_trend_breakeven_min_live_gap_percent")
                    or (cfg or {}).get("adaptive_trend_breakeven_min_live_gap_percent", 0.30)
                    or 0.30
                ),
            ),
        )
    except (TypeError, ValueError):
        min_gap = 0.30
    try:
        attempts = max(2, min(12, int((cfg or {}).get("adaptive_trend_stop_verify_attempts", 6) or 6)))
    except (TypeError, ValueError):
        attempts = 6
    try:
        delay_sec = max(0.0, min(1.0, float((cfg or {}).get("adaptive_trend_stop_verify_delay_sec", 0.35) or 0.35)))
    except (TypeError, ValueError):
        delay_sec = 0.35

    snapshot = await read_exchange_sl_snapshot(engine, symbol, side)
    exchange_stop = best_live_exchange_stop(snapshot, side, mark) if snapshot.get("fetch_ok") else None
    safe_before_stop = _as_float(before_stop)
    if not _stop_live(side, safe_before_stop, mark):
        safe_before_stop = None
    required = _required_stop(
        side,
        avg_entry,
        cost_buffer,
        safe_before_stop,
        exchange_stop,
        mark,
        min_gap,
    )
    if required is None or not _stop_live(side, required, mark):
        return await _emergency_close(engine, symbol, "adaptive trend pyramid no live post-fill SL target")

    verified = _verified_entry(engine, snapshot, side, required, live_qty) if snapshot.get("fetch_ok") else None
    final_snapshot = snapshot
    if verified is None:
        verified, final_snapshot = await _replace_then_verify(
            engine,
            symbol,
            live_pos,
            side,
            required,
            live_qty,
            snapshot,
            attempts,
            delay_sec,
            "Adaptive Trend pyramid full-quantity profit-lock",
        )

    if verified is None:
        # If the tighter BE target crossed or the new order was briefly absent,
        # rebuild the best still-live stop for the enlarged quantity before any
        # emergency close. This is the key anti-false-liquidation fallback.
        live_exchange_fallback = (
            best_live_exchange_stop(final_snapshot, side, mark)
            if final_snapshot.get("fetch_ok")
            else None
        )
        fallback_candidates = [
            stop
            for stop in (safe_before_stop, live_exchange_fallback)
            if _stop_live(side, stop, mark)
        ]
        fallback = None
        if fallback_candidates:
            fallback = max(fallback_candidates) if side == "long" else min(fallback_candidates)
        if fallback is not None:
            verified, final_snapshot = await _replace_then_verify(
                engine,
                symbol,
                live_pos,
                side,
                fallback,
                live_qty,
                final_snapshot,
                attempts,
                delay_sec,
                "Adaptive Trend pyramid restore full-quantity live SL",
            )
            if verified is not None:
                required = fallback

    if verified is None:
        return await _emergency_close(
            engine,
            symbol,
            "adaptive trend pyramid full-quantity exchange SL could not be verified",
        )

    actual_stop = float(verified["stop"])
    risk_anchor = (
        _as_float(state.get("adaptive_trend_initial_risk_distance"))
        or _as_float(state.get("risk_distance"))
        or abs(avg_entry - actual_stop)
    )
    applied_buffer = max(
        0.0,
        (actual_stop - avg_entry) / avg_entry * 100.0
        if side == "long"
        else (avg_entry - actual_stop) / avg_entry * 100.0,
    )
    state.update(
        {
            "active": True,
            "breakeven_armed": True,
            "entry_price": avg_entry,
            "initial_qty": live_qty,
            "risk_distance": risk_anchor,
            "stop_loss": actual_stop,
            "hard_stop_loss": actual_stop,
            "last_stop_price": actual_stop,
            "adaptive_trend_breakeven_cost_buffer_percent": cost_buffer,
            "adaptive_trend_breakeven_applied_cost_buffer_percent": applied_buffer,
            "adaptive_trend_breakeven_min_live_gap_percent": min_gap,
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
    _mark_latest_position_add_protected(engine, symbol, verified.get("order"))
    if str(getattr(engine, "crypto_entry_lock_reason", "") or "").startswith("FILLED_"):
        _set_entry_lock(engine, None)

    output = dict(result)
    if geometry_repair:
        output.update(
            {
                "status": "ADDED_PROTECTION_REPAIRED",
                "reason": "adaptive trend pyramid exchange SL repaired after filled add",
            }
        )
    output.update(
        {
            "post_add_stop_guard": "OK",
            "exchange_stop_verified": True,
            "required_stop": required,
            "verified_stop": actual_stop,
            "position_qty": live_qty,
        }
    )
    return output


__all__ = (
    "best_live_exchange_stop",
    "enforce_adaptive_pyramid_live_sl_guard",
    "read_exchange_sl_snapshot",
)

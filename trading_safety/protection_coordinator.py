"""Serialize the complete protection lifecycle for each live symbol."""

from __future__ import annotations

import asyncio
from typing import Any


_TERMINAL_TRAILING_STATUSES = {
    "EXITED",
    "EXIT_PENDING",
    "FLAT_CLEANED",
    "FLAT_CLEANUP_PENDING",
    "FLAT_ACCOUNTING_PENDING",
}


def _lock_key(engine: Any, symbol: str) -> str:
    canonicalizer = getattr(engine, "_canonicalize_utbreakout_symbol_for_use", None)
    if callable(canonicalizer):
        try:
            return str(canonicalizer(symbol, source="protection_coordinator"))
        except Exception:
            pass
    return str(symbol or "").strip().upper()


def _symbol_lock(engine: Any, symbol: str) -> asyncio.Lock:
    locks = getattr(engine, "_utbreakout_protection_locks", None)
    if not isinstance(locks, dict):
        locks = {}
        setattr(engine, "_utbreakout_protection_locks", locks)
    key = _lock_key(engine, symbol)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


async def manage_open_position_protection(
    engine: Any,
    symbol: str,
    position: dict[str, Any],
    candles: Any,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run trailing, winner-add, and ladder management as one transaction.

    Both the frequent open-position poll and the closed-candle processor can
    reach these routines.  A per-symbol lock prevents one path from auditing
    or cancelling orders while the other path is rebuilding protection.
    """

    lock = _symbol_lock(engine, symbol)
    waited_for_existing_transaction = lock.locked()
    async with lock:
        current_position = position
        if waited_for_existing_transaction:
            fetch_ok, refreshed = await engine._fetch_server_position_checked(symbol)
            if not fetch_ok:
                return {
                    "status": "POSITION_REFRESH_FAILED",
                    "position": current_position,
                    "terminal": True,
                }
            if not refreshed:
                return {
                    "status": "POSITION_FLAT_AFTER_WAIT",
                    "position": None,
                    "terminal": True,
                }
            current_position = refreshed

        state = engine._get_utbreakout_trailing_state(symbol)
        advanced_ladder = bool(
            isinstance(state, dict) and state.get("advanced_live_ladder_state")
        )
        trailing_result = None
        if not advanced_ladder:
            trailing_result = await engine._manage_utbreakout_partial_trailing(
                symbol,
                current_position,
                candles,
                config,
            )
            trailing_status = str(
                (trailing_result or {}).get("status")
                if isinstance(trailing_result, dict)
                else ""
            ).upper()
            if trailing_status in _TERMINAL_TRAILING_STATUSES:
                return {
                    "status": trailing_status,
                    "position": None if trailing_status in {"EXITED", "FLAT_CLEANED"} else current_position,
                    "terminal": True,
                    "trailing": trailing_result,
                }

        pyramid_status = await engine._maybe_apply_aggressive_growth_pyramiding(
            symbol,
            current_position,
            candles,
            config,
        )
        if isinstance(pyramid_status, dict) and pyramid_status.get("status") == "ADDED":
            fetch_ok, refreshed = await engine._fetch_server_position_checked(symbol)
            if fetch_ok and refreshed:
                current_position = refreshed

        state = engine._get_utbreakout_trailing_state(symbol)
        advanced_ladder = bool(
            isinstance(state, dict) and state.get("advanced_live_ladder_state")
        )
        ladder_result = None
        if advanced_ladder:
            ladder_result = await engine._manage_live_ladder_exit_policy(
                symbol,
                current_position,
                candles,
                config,
            )

        return {
            "status": "MANAGED",
            "position": current_position,
            "terminal": False,
            "trailing": trailing_result,
            "pyramid": pyramid_status,
            "ladder": ladder_result,
        }

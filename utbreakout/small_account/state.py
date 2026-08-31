"""Explicit live-protection state for the small-account strategy.

``active`` remains a trailing/profit-lock activation flag for compatibility.
It must never be used to decide whether a protective order belongs to the bot.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


PROTECTION_REGISTERED = "REGISTERED"
PROTECTION_PROTECTED = "PROTECTED"


def apply_protection_state_contract(
    state: MutableMapping[str, Any],
    *,
    existing_state: Mapping[str, Any] | None = None,
    stop_price: Any = None,
) -> MutableMapping[str, Any]:
    """Attach order-ownership fields without changing trailing activation.

    Re-registering state after a profitable pyramid add intentionally rebuilds
    several price/quantity fields.  The bot-managed/protected identity is not a
    trailing stage, so it must survive that rebuild independently of ``active``.
    """

    previous = existing_state if isinstance(existing_state, Mapping) else {}
    parsed_stop = _positive_float(stop_price)
    if parsed_stop is None:
        parsed_stop = _positive_float(state.get("last_stop_price"))
    if parsed_stop is None:
        parsed_stop = _positive_float(previous.get("last_stop_price"))

    state["managed_position"] = True
    state["protection_contract_version"] = 1
    state["profit_lock_armed"] = bool(
        state.get("active", False) or previous.get("active", False)
    )
    state["protection_status"] = (
        PROTECTION_PROTECTED if parsed_stop is not None else PROTECTION_REGISTERED
    )
    if parsed_stop is not None:
        state["protected_stop_price"] = parsed_stop
    return state


def is_managed_position_state(
    state: Mapping[str, Any] | None,
    *,
    side: str,
) -> bool:
    """Return whether state identifies a bot-managed position for ``side``."""

    if not isinstance(state, Mapping):
        return False
    state_side = str(state.get("side") or "").strip().lower()
    if state_side != str(side or "").strip().lower():
        return False
    if state.get("managed_position") is not None:
        return bool(state.get("managed_position"))
    # Backward compatibility for persisted states created before contract v1.
    # Presence of a complete trailing state means the bot adopted and manages
    # this position even when the trailing stage itself has not activated yet.
    return bool(
        _positive_float(state.get("entry_price")) is not None
        and _positive_float(state.get("last_stop_price")) is not None
    )


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None

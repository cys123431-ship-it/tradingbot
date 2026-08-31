"""Post-fill strategy policy for accounts using the aggressive profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def small_account_strategy_active(state: Mapping[str, Any] | None) -> bool:
    """Return whether the filled position owns the small-account profile."""

    return bool(
        isinstance(state, Mapping)
        and state.get("small_account_aggressive_active", False)
    )


def small_account_profit_lock_enabled(state: Mapping[str, Any] | None) -> bool:
    """Return the strategy intent; order validity remains a risk concern."""

    return bool(
        small_account_strategy_active(state)
        and isinstance(state, Mapping)
        and state.get("small_account_roe_profit_lock_enabled", False)
    )

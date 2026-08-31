"""Small-account strategy/risk contracts shared by live execution modules.

The signal engine decides *whether* to trade.  This package owns the explicit
state and stop-geometry contracts used after a small-account order is filled.
Keeping those meanings separate prevents strategy flags from being reused as
order-ownership flags.
"""

from .risk import StopGeometry, classify_stop_geometry, position_mark_price
from .state import (
    PROTECTION_PROTECTED,
    PROTECTION_REGISTERED,
    apply_protection_state_contract,
    is_managed_position_state,
)
from .strategy import (
    small_account_profit_lock_enabled,
    small_account_strategy_active,
)

__all__ = [
    "PROTECTION_PROTECTED",
    "PROTECTION_REGISTERED",
    "StopGeometry",
    "apply_protection_state_contract",
    "classify_stop_geometry",
    "is_managed_position_state",
    "position_mark_price",
    "small_account_profit_lock_enabled",
    "small_account_strategy_active",
]

"""Service for the /macross strategy (SMA3/SMA120, C-only, TradFi-capable).

Reuses the battle-tested live plumbing of the 3/200 SMA service (account
setup, order gateway, protection orders, reconciliation, paper trading) and
only swaps the signal + sizing through ``normalize_config``.  TradFi scope
(mainnet-only, US equities + gold, US regular-session gate) comes from the
shared ``tradfi`` module so both strategies classify markets identically.
"""

from __future__ import annotations

import logging

from btc_ma_cross.service import BtcMaCrossService

from .config import CLIENT_ID_STRATEGY, STRATEGY_VERSION, normalize_ma_cross_config
from .tradfi import TradFiScopeMixin

logger = logging.getLogger(__name__)


class MaCrossService(TradFiScopeMixin, BtcMaCrossService):
    """SMA3/SMA120 with C-only sizing and the opposite-cross exit."""

    CLIENT_ID = CLIENT_ID_STRATEGY
    RUNTIME_SUBDIR = "ma_cross"
    LABEL = "SMA3/120 C"
    # C-only: exits are the opposite cross and the profit lock; no emergency stop.
    PROTECTION_KINDS = ("lock",)
    EXIT_REASON_BY_KIND = {"lock": "PROFIT_LOCK"}
    normalize_config = staticmethod(normalize_ma_cross_config)
    STRATEGY_VERSION = STRATEGY_VERSION


__all__ = ("MaCrossService",)

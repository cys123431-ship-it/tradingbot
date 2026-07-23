"""Automatic-strategy-only entry controls."""

from __future__ import annotations

import asyncio

from utbreakout.coinselector import market_is_tradifi_perpetual

from .controller_automatic_controls import (
    AUTOMATIC_DAILY_TRADE_LIMIT_BASE,
    AUTOMATIC_SCAN_SCOPE_ALL,
    AUTOMATIC_SCAN_SCOPE_CRYPTO,
    AUTOMATIC_SCAN_SCOPE_TRADIFI,
)


class SignalAutomaticControlsMixin:
    """Enforce daily and universe controls only for automatic entries."""

    def get_effective_automatic_daily_trade_limit(self):
        ctrl = getattr(self, "ctrl", None)
        if ctrl is not None and hasattr(
            ctrl, "get_effective_automatic_daily_trade_limit"
        ):
            return int(ctrl.get_effective_automatic_daily_trade_limit())
        return AUTOMATIC_DAILY_TRADE_LIMIT_BASE

    def get_automatic_scan_scope(self):
        ctrl = getattr(self, "ctrl", None)
        if ctrl is not None and hasattr(ctrl, "get_automatic_scan_scope"):
            return str(ctrl.get_automatic_scan_scope())
        return AUTOMATIC_SCAN_SCOPE_ALL

    def get_automatic_daily_entry_count(self):
        db = getattr(self, "db", None)
        if db is None:
            return 0
        if hasattr(db, "get_daily_automatic_entry_count"):
            return int(db.get_daily_automatic_entry_count())
        if hasattr(db, "get_daily_entry_count"):
            return int(db.get_daily_entry_count())
        return 0

    async def _assert_automatic_entry_scan_scope(self, symbol):
        """Fail closed when an automatic entry falls outside the selected scope."""
        if self.is_upbit_mode():
            return symbol
        scope = self.get_automatic_scan_scope()
        if scope == AUTOMATIC_SCAN_SCOPE_ALL:
            return symbol

        ctrl = getattr(self, "ctrl", None)
        if ctrl is None:
            raise ValueError(
                "REJECTED_SCAN_SCOPE_UNCLASSIFIED: automatic controller unavailable"
            )
        if hasattr(ctrl, "_load_trade_markets_for_exchange_mode"):
            markets = await ctrl._load_trade_markets_for_exchange_mode(
                ctrl.get_exchange_mode()
            )
        else:
            exchange = getattr(self, "exchange", None)
            if exchange is None or not hasattr(exchange, "load_markets"):
                raise ValueError(
                    "REJECTED_SCAN_SCOPE_UNCLASSIFIED: market metadata unavailable"
                )
            markets = await asyncio.to_thread(exchange.load_markets)
        market = (
            ctrl._futures_market_for_symbol(symbol, markets)
            if hasattr(ctrl, "_futures_market_for_symbol")
            else None
        )
        if not isinstance(market, dict):
            raise ValueError(
                f"REJECTED_SCAN_SCOPE_UNCLASSIFIED: cannot classify {symbol}"
            )
        market_symbol = market.get("symbol") or symbol
        is_tradifi = market_is_tradifi_perpetual(market_symbol, market)
        if scope == AUTOMATIC_SCAN_SCOPE_TRADIFI and not is_tradifi:
            raise ValueError(
                f"REJECTED_SCAN_SCOPE_CRYPTO: TradFi ONLY excludes {symbol}"
            )
        if scope == AUTOMATIC_SCAN_SCOPE_CRYPTO and is_tradifi:
            raise ValueError(
                f"REJECTED_SCAN_SCOPE_TRADIFI: crypto-only scope excludes {symbol}"
            )
        return market_symbol


__all__ = ("SignalAutomaticControlsMixin",)

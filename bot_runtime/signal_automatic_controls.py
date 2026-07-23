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


class _AutomaticDailyEntryCount(int):
    """Keep the real count while honoring today's effective limit in legacy guards.

    Some older strategy paths still compare the automatic entry count against the
    fixed base limit (5).  When today's Telegram extension is active, reinterpret
    only those ``count >= 5`` checks as ``count >= 10``.  ``int(count)`` and normal
    display still expose the real database count, so the final order gateway and
    status messages remain accurate.
    """

    def __new__(cls, value, *, effective_limit):
        obj = super().__new__(cls, max(0, int(value or 0)))
        obj.effective_limit = max(
            AUTOMATIC_DAILY_TRADE_LIMIT_BASE,
            int(effective_limit or AUTOMATIC_DAILY_TRADE_LIMIT_BASE),
        )
        return obj

    def __ge__(self, other):
        try:
            comparison_limit = int(other)
        except (TypeError, ValueError):
            return NotImplemented
        if (
            comparison_limit == AUTOMATIC_DAILY_TRADE_LIMIT_BASE
            and self.effective_limit > AUTOMATIC_DAILY_TRADE_LIMIT_BASE
        ):
            comparison_limit = self.effective_limit
        return int(self) >= comparison_limit

    def __gt__(self, other):
        try:
            comparison_limit = int(other)
        except (TypeError, ValueError):
            return NotImplemented
        if (
            comparison_limit == AUTOMATIC_DAILY_TRADE_LIMIT_BASE
            and self.effective_limit > AUTOMATIC_DAILY_TRADE_LIMIT_BASE
        ):
            comparison_limit = self.effective_limit
        return int(self) > comparison_limit


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
        count = 0
        if db is not None:
            if hasattr(db, "get_daily_automatic_entry_count"):
                count = int(db.get_daily_automatic_entry_count())
            elif hasattr(db, "get_daily_entry_count"):
                count = int(db.get_daily_entry_count())
        return _AutomaticDailyEntryCount(
            count,
            effective_limit=self.get_effective_automatic_daily_trade_limit(),
        )

    async def _calculate_utbot_filtered_breakout_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        """Normalize the legacy UTBreak daily-count rejection after evaluation."""
        result = await super()._calculate_utbot_filtered_breakout_signal(
            symbol,
            df,
            strategy_params,
            force_reprocess=force_reprocess,
        )
        try:
            signal, reason, status = result
        except (TypeError, ValueError):
            return result

        legacy_prefix = "REJECTED_DAILY_LOSS_LIMIT: daily trade count"
        if signal is not None or legacy_prefix not in str(reason or ""):
            return result

        effective_limit = self.get_effective_automatic_daily_trade_limit()
        daily_entries = int(self.get_automatic_daily_entry_count())
        corrected_reason = (
            "REJECTED_DAILY_TRADE_LIMIT: daily trade count "
            f"{daily_entries} >= {effective_limit}"
        )
        corrected_status = dict(status or {})
        corrected_status["reason"] = corrected_reason
        corrected_status["reject_code"] = "REJECTED_DAILY_TRADE_LIMIT"

        if hasattr(self, "_store_utbot_filtered_breakout_status"):
            self._store_utbot_filtered_breakout_status(symbol, corrected_status)
        last_reasons = getattr(self, "last_entry_reason", None)
        if isinstance(last_reasons, dict):
            last_reasons[symbol] = corrected_reason

        return signal, corrected_reason, corrected_status

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

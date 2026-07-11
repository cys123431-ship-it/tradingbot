"""Finalize live-trade fees/funding and rebuild final-only engine statistics."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
import math
from typing import Any

from .order_state import SQLiteTradingStateStore


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


class TradeAccountingFinalizer:
    def __init__(self, exchange: Any, store: SQLiteTradingStateStore) -> None:
        self.exchange = exchange
        self.store = store

    async def finalize_trade(self, trade_id: str) -> dict[str, Any]:
        trade = self.store.get_trade_result(trade_id)
        if trade is None:
            raise KeyError(trade_id)
        symbol = str(trade.get("symbol") or "")
        since = None
        until = None
        entry_time = trade.get("entry_time")
        if entry_time:
            try:
                since = int(datetime.fromisoformat(str(entry_time).replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                since = None
        exit_time = trade.get("exit_time")
        if exit_time:
            try:
                until = int(datetime.fromisoformat(str(exit_time).replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                until = None
        fetch_trades = getattr(self.exchange, "fetch_my_trades", None)
        if not callable(fetch_trades):
            return self._persist_pending(trade, "fetch_my_trades unavailable")
        try:
            fills = await asyncio.to_thread(fetch_trades, symbol, since)
        except Exception as exc:
            return self._persist_pending(trade, f"trade fill lookup failed: {type(exc).__name__}:{exc}")
        related = []
        for item in fills or []:
            if not isinstance(item, dict):
                continue
            raw_item_info = item.get("info")
            item_info = raw_item_info if isinstance(raw_item_info, dict) else {}
            timestamp = int(_number(item.get("timestamp") or item_info.get("time")))
            if since is not None and timestamp and timestamp < since:
                continue
            if until is not None and timestamp and timestamp > until + 60_000:
                continue
            related.append(item)
        if not related:
            return self._persist_pending(trade, "no exchange fills found")

        entry_side = "buy" if str(trade.get("side") or "").lower() == "long" else "sell"
        entry_fees = 0.0
        exit_fees = 0.0
        for fill in related:
            raw_fee = fill.get("fee")
            raw_info = fill.get("info")
            fee = raw_fee if isinstance(raw_fee, dict) else {}
            info = raw_info if isinstance(raw_info, dict) else {}
            commission = abs(_number(fee.get("cost") or info.get("commission")))
            if str(fill.get("side") or info.get("side") or "").lower() == entry_side:
                entry_fees += commission
            else:
                exit_fees += commission

        funding = 0.0
        income_method = getattr(self.exchange, "fapiPrivateGetIncome", None)
        if callable(income_method):
            try:
                params: dict[str, Any] = {"symbol": symbol.replace("/", "").split(":", 1)[0]}
                if since:
                    params["startTime"] = since
                income_rows = await asyncio.to_thread(income_method, params)
                funding = sum(
                    _number(row.get("income"))
                    for row in income_rows or []
                    if isinstance(row, dict) and row.get("incomeType") == "FUNDING_FEE"
                )
            except Exception as exc:
                return self._persist_pending(trade, f"funding lookup failed: {type(exc).__name__}:{exc}")

        gross = _number(trade.get("gross_pnl_usdt"))
        slippage = _number(trade.get("slippage_usdt"))
        if trade.get("slippage_usdt") is None:
            qty = abs(_number(trade.get("filled_qty")))
            side = str(trade.get("side") or "").lower()
            planned_entry = _number(trade.get("planned_entry_price"))
            actual_entry = _number(trade.get("entry_price"))
            exit_reference = _number(trade.get("exit_reference_price"))
            actual_exit = _number(trade.get("exit_price"))
            if qty > 0 and planned_entry > 0 and actual_entry > 0:
                slippage += qty * max(
                    0.0,
                    actual_entry - planned_entry if side == "long" else planned_entry - actual_entry,
                )
            if qty > 0 and exit_reference > 0 and actual_exit > 0:
                slippage += qty * max(
                    0.0,
                    exit_reference - actual_exit if side == "long" else actual_exit - exit_reference,
                )
        finalized = dict(trade)
        finalized.update(
            {
                "entry_fee_usdt": entry_fees,
                "exit_fee_usdt": exit_fees,
                "funding_usdt": funding,
                "slippage_usdt": slippage,
                "net_pnl_usdt": gross - entry_fees - exit_fees + funding - slippage,
                "provisional": False,
                "accounting_revision": int(trade.get("accounting_revision") or 0) + 1,
                "last_accounting_error": None,
            }
        )
        self.store.upsert_trade_result(finalized)
        return finalized

    def _persist_pending(self, trade: dict[str, Any], error: str) -> dict[str, Any]:
        pending = dict(trade)
        pending["provisional"] = True
        pending["last_accounting_error"] = error
        self.store.upsert_trade_result(pending)
        return pending

    async def finalize_pending(self, limit: int = 10) -> list[dict[str, Any]]:
        results = []
        for trade in self.store.load_provisional_trade_results(limit):
            trade_id = str(trade.get("trade_id") or "")
            if trade_id:
                results.append(await self.finalize_trade(trade_id))
        return results


def rebuild_engine_performance_stats(
    finalized_trades: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in finalized_trades:
        if trade.get("provisional"):
            continue
        engine = str(trade.get("engine") or "")
        if engine and engine != "NONE":
            grouped[engine].append(trade)
    result: dict[str, dict[str, Any]] = {}
    for engine, trades in grouped.items():
        trades.sort(key=lambda item: str(item.get("exit_time") or ""))
        realized = [_number(item.get("realized_r", item.get("r_multiple"))) for item in trades]
        net_values = [_number(item.get("net_pnl_usdt")) for item in trades]
        gross_profit_r = sum(value for value in realized if value > 0)
        gross_loss_r = abs(sum(value for value in realized if value < 0))
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        consecutive = 0
        max_consecutive = 0
        for value in net_values:
            equity += value
            peak = max(peak, equity)
            denominator = max(abs(peak), 1.0)
            max_drawdown = max(max_drawdown, (peak - equity) / denominator * 100.0)
        for value in realized:
            consecutive = consecutive + 1 if value < 0 else 0
            max_consecutive = max(max_consecutive, consecutive)
        fees = sum(
            abs(_number(item.get("entry_fee_usdt"))) + abs(_number(item.get("exit_fee_usdt")))
            for item in trades
        )
        funding_cost = sum(max(0.0, -_number(item.get("funding_usdt"))) for item in trades)
        gross_profit_usdt = sum(max(0.0, _number(item.get("gross_pnl_usdt"))) for item in trades)
        denominator = max(abs(gross_profit_usdt), 1e-12)
        result[engine] = {
            "trade_count": len(trades),
            "expectancy_r": sum(realized) / len(realized) if realized else 0.0,
            "profit_factor": (
                gross_profit_r / gross_loss_r
                if gross_loss_r > 0
                else (float("inf") if gross_profit_r > 0 else 0.0)
            ),
            "max_drawdown_pct": max_drawdown,
            "max_consecutive_losses": max_consecutive,
            "consecutive_losses": consecutive,
            "fee_burden": fees / denominator,
            "funding_burden": funding_cost / denominator,
            "gross_profit_r": gross_profit_r,
            "gross_loss_r": gross_loss_r,
            "net_profit_usdt": sum(net_values),
            "oos_expectancy": None,
        }
    return result

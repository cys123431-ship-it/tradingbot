"""Finalize live-trade fees/funding and rebuild final-only engine statistics."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import logging
import math
import re
import time
from typing import Any

from .order_state import SQLiteTradingStateStore


logger = logging.getLogger(__name__)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


async def resolve_closed_trade_accounting(
    engine: Any,
    symbol: str,
    open_trade: dict[str, Any],
    *,
    exit_price: Any = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve realized PnL and individual exit legs from exchange fills."""

    entry_price = float(open_trade.get("entry_price", 0) or 0)
    entry_qty = abs(float(open_trade.get("quantity", 0) or 0))
    side = str(open_trade.get("side", "") or "").lower()
    close_side = "sell" if side == "long" else "buy"
    entry_time = open_trade.get("entry_time")
    since_ms = None
    try:
        since_ms = int(
            datetime.fromisoformat(
                str(entry_time).replace("Z", "+00:00")
            ).timestamp()
            * 1000
        )
    except (TypeError, ValueError):
        pass

    fetch_my_trades = getattr(engine.exchange, "fetch_my_trades", None)
    if callable(fetch_my_trades):
        try:
            trades = await asyncio.to_thread(
                fetch_my_trades,
                symbol,
                since_ms,
                1000,
            )
            realized_pnl = 0.0
            realized_seen = False
            weighted_exit = 0.0
            closing_qty = 0.0
            exit_legs = []
            state_data = state if isinstance(state, dict) else {}
            planned_targets = list(
                state_data.get("planned_tp_orders")
                or state_data.get("tp_orders")
                or []
            )
            entry_record = engine._utbreakout_entry_record_for_symbol(
                symbol,
                include_historic=True,
            )
            entry_metadata = dict(
                getattr(entry_record, "metadata", {}) or {}
            )
            persisted_tp_labels = {
                str(order_id): str(label).upper()
                for order_id, label in dict(
                    entry_metadata.get("take_profit_order_labels") or {}
                ).items()
                if order_id not in (None, "") and label not in (None, "")
            }
            persisted_tp_ids = [
                str(value)
                for value in (
                    getattr(entry_record, "take_profit_order_ids", None)
                    or []
                )
                if value not in (None, "")
            ]
            for index, order_id in enumerate(persisted_tp_ids, 1):
                persisted_tp_labels.setdefault(order_id, f"TP{index}")
            persisted_stop_ids = {
                str(value)
                for value in (
                    getattr(entry_record, "stop_order_id", None),
                    state_data.get("sl_order_id"),
                )
                if value not in (None, "")
            }

            def _fill_label(order_id, fill_price):
                order_id_text = str(order_id or "")
                if order_id_text in persisted_tp_labels:
                    return persisted_tp_labels[order_id_text]
                if order_id_text in persisted_stop_ids:
                    return "SL"
                for target in planned_targets:
                    if not isinstance(target, dict):
                        continue
                    target_ids = {
                        str(target.get(key) or "")
                        for key in ("order_id", "id", "client_order_id")
                    }
                    if order_id_text and order_id_text in target_ids:
                        return str(
                            target.get("tp_label")
                            or target.get("tp_name")
                            or f"TP{target.get('tp_index') or ''}"
                        ).upper()
                priced_targets = []
                for target in planned_targets:
                    if not isinstance(target, dict):
                        continue
                    target_price = _number_or_none(target.get("price"))
                    if target_price and fill_price > 0:
                        priced_targets.append(
                            (abs(fill_price / target_price - 1.0), target)
                        )
                if priced_targets:
                    distance, target = min(
                        priced_targets,
                        key=lambda item: item[0],
                    )
                    if distance <= 0.003:
                        return str(
                            target.get("tp_label")
                            or target.get("tp_name")
                            or f"TP{target.get('tp_index') or ''}"
                        ).upper()
                stop_price = _number_or_none(
                    state_data.get("last_stop_price")
                    or state_data.get("hard_stop_price")
                )
                if (
                    stop_price
                    and fill_price > 0
                    and abs(fill_price / stop_price - 1.0) <= 0.005
                ):
                    return "SL"
                return "EXIT"

            for trade in trades or []:
                info = (
                    trade.get("info")
                    if isinstance(trade.get("info"), dict)
                    else {}
                )
                trade_side = str(
                    trade.get("side") or info.get("side") or ""
                ).lower()
                if trade_side != close_side:
                    continue
                position_side = str(
                    info.get("positionSide")
                    or trade.get("positionSide")
                    or "BOTH"
                ).upper()
                if side == "long" and position_side not in {
                    "LONG",
                    "BOTH",
                    "",
                }:
                    continue
                if side == "short" and position_side not in {
                    "SHORT",
                    "BOTH",
                    "",
                }:
                    continue
                trade_ts = trade.get("timestamp") or info.get("time")
                try:
                    if (
                        since_ms is not None
                        and trade_ts is not None
                        and int(trade_ts) < since_ms
                    ):
                        continue
                except (TypeError, ValueError):
                    continue
                pnl_value = trade.get("realizedPnl")
                if pnl_value in (None, ""):
                    pnl_value = info.get("realizedPnl")
                if pnl_value not in (None, ""):
                    realized_pnl += float(pnl_value)
                    realized_seen = True
                qty_value = trade.get("amount")
                if qty_value in (None, ""):
                    qty_value = info.get("qty")
                price_value = trade.get("price")
                if price_value in (None, ""):
                    price_value = info.get("price")
                try:
                    fill_qty = abs(float(qty_value or 0))
                    fill_price = float(price_value or 0)
                except (TypeError, ValueError):
                    fill_qty = 0.0
                    fill_price = 0.0
                if fill_qty > 0 and fill_price > 0:
                    closing_qty += fill_qty
                    weighted_exit += fill_qty * fill_price
                    exit_legs.append(
                        {
                            "label": _fill_label(
                                trade.get("order")
                                or info.get("orderId")
                                or info.get("order"),
                                fill_price,
                            ),
                            "timestamp": trade_ts,
                            "order_id": trade.get("order")
                            or info.get("orderId")
                            or info.get("order"),
                            "qty": fill_qty,
                            "price": fill_price,
                            "realized_pnl_usdt": float(pnl_value or 0.0),
                        }
                    )
            if realized_seen and closing_qty > 0:
                resolved_exit = weighted_exit / closing_qty
                cost_basis = entry_price * max(entry_qty, closing_qty)
                pnl_pct = (
                    realized_pnl / cost_basis * 100.0
                    if cost_basis > 0
                    else 0.0
                )
                return {
                    "pnl": float(realized_pnl),
                    "pnl_pct": float(pnl_pct),
                    "exit_price": float(resolved_exit),
                    "estimated": False,
                    "source": "exchange_trades",
                    "exit_legs": exit_legs,
                }
        except Exception as exc:
            logger.warning(
                "Closed-trade fill lookup failed for %s: %s",
                symbol,
                exc,
            )

    fallback_exit = _number_or_none(exit_price)
    if fallback_exit is None and isinstance(state, dict):
        fallback_exit = _number_or_none(state.get("last_close"))
    if fallback_exit is None:
        try:
            ticker = await asyncio.to_thread(engine.exchange.fetch_ticker, symbol)
            fallback_exit = _number_or_none(
                (ticker or {}).get("last")
                or (ticker or {}).get("mark")
                or (ticker or {}).get("close")
            )
        except Exception as exc:
            logger.warning(
                "Closed-trade fallback ticker failed for %s: %s",
                symbol,
                exc,
            )
    if fallback_exit is None and isinstance(state, dict):
        fallback_exit = _number_or_none(state.get("last_stop_price"))
    if (
        fallback_exit is None
        or entry_price <= 0
        or entry_qty <= 0
        or side not in {"long", "short"}
    ):
        return None
    pnl = (
        (float(fallback_exit) - entry_price) * entry_qty
        if side == "long"
        else (entry_price - float(fallback_exit)) * entry_qty
    )
    pnl_pct = (
        (float(fallback_exit) - entry_price) / entry_price * 100.0
        if side == "long"
        else (entry_price - float(fallback_exit)) / entry_price * 100.0
    )
    return {
        "pnl": float(pnl),
        "pnl_pct": float(pnl_pct),
        "exit_price": float(fallback_exit),
        "estimated": True,
        "source": "fallback_price",
        "exit_legs": [
            {
                "label": "EXIT",
                "timestamp": int(time.time() * 1000),
                "order_id": None,
                "qty": entry_qty,
                "price": float(fallback_exit),
                "realized_pnl_usdt": float(pnl),
            }
        ],
    }


async def record_closed_trade_accounting(
    engine: Any,
    symbol: str,
    reason: str,
    *,
    exit_price: Any = None,
    state: dict[str, Any] | None = None,
    record_realized_pnl=None,
    persist_live_trade=None,
    ensure_trading_runtime=None,
) -> dict[str, Any]:
    """Close the DB trade and persist the provisional live result."""

    db = getattr(engine, "db", None)
    if db is None or not hasattr(db, "get_latest_open_trade"):
        return {"status": "NO_DB"}
    try:
        symbol_candidates = list(engine._utbreakout_plan_symbol_keys(symbol))
    except Exception:
        symbol_candidates = [str(symbol or "")]
    raw_symbol = str(symbol or "")
    if raw_symbol and raw_symbol not in symbol_candidates:
        symbol_candidates.insert(0, raw_symbol)
    if raw_symbol.endswith("/USDT"):
        symbol_candidates.append(raw_symbol + ":USDT")
    open_trade = None
    accounting_symbol = raw_symbol
    for candidate in dict.fromkeys(
        item for item in symbol_candidates if item
    ):
        open_trade = db.get_latest_open_trade(candidate)
        if open_trade:
            accounting_symbol = candidate
            break
    if not open_trade:
        return {"status": "NO_OPEN_TRADE"}
    result = await resolve_closed_trade_accounting(
        engine,
        symbol,
        open_trade,
        exit_price=exit_price,
        state=state,
    )
    if not result:
        logger.error(
            "Automatic close accounting unresolved for %s; DB row remains open",
            symbol,
        )
        return {"status": "UNRESOLVED"}
    close_reason = str(reason or "automatic close")
    if result.get("estimated"):
        close_reason = f"{close_reason} [estimated-price]"
    reason_upper = close_reason.upper()
    generic_flat_reason = (
        "TAKE PROFIT/STOP LOSS CLOSED POSITION" in reason_upper
        or "AUTOMATIC PROTECTION FILL" in reason_upper
    )
    exit_legs = [
        dict(item)
        for item in result.get("exit_legs") or []
        if isinstance(item, dict)
    ]
    for leg in exit_legs:
        if str(leg.get("label") or "").upper() != "EXIT":
            continue
        if "MANUAL" in reason_upper or "EMERGENCY" in reason_upper:
            leg["label"] = "MANUAL"
        elif "TP1" in reason_upper:
            leg["label"] = "TP1"
        elif "TP2" in reason_upper or (
            "TAKE_PROFIT" in reason_upper and not generic_flat_reason
        ):
            leg["label"] = "TP2"
        elif any(
            token in reason_upper
            for token in ("TRAIL", "RUNNER", "CHANDELIER")
        ):
            leg["label"] = "RUNNER"
        elif not generic_flat_reason and (
            "STOP" in reason_upper
            or re.search(r"(^|[^A-Z])SL([^A-Z]|$)", reason_upper)
        ):
            leg["label"] = "SL"
        elif "TIME" in reason_upper:
            leg["label"] = "TIME_STOP"
        elif generic_flat_reason:
            leg["label"] = "EXTERNAL_EXIT"
    result["exit_legs"] = exit_legs
    updated = db.log_trade_close(
        accounting_symbol,
        result["pnl"],
        result["pnl_pct"],
        result["exit_price"],
        close_reason,
    )
    result["status"] = (
        "RECORDED" if updated is not False else "NO_OPEN_TRADE"
    )
    if updated is False:
        return result
    if callable(record_realized_pnl):
        try:
            record_realized_pnl(result.get("pnl", 0.0))
        except Exception:
            logger.exception(
                "Bot realized PnL state update failed for %s",
                symbol,
            )
    try:
        engine._record_utbreakout_recent_loss_cooldown(
            accounting_symbol,
            side=(open_trade or {}).get("side"),
            pnl_usdt=result.get("pnl"),
            reason=close_reason,
        )
    except Exception:
        logger.debug(
            "UTBreakout recent loss cooldown record failed for %s",
            accounting_symbol,
            exc_info=True,
        )
    try:
        state_data = state if isinstance(state, dict) else {}
        risk_distance = float(state_data.get("risk_distance") or 0.0)
        filled_qty = float((open_trade or {}).get("quantity") or 0.0)
        risk_usdt = risk_distance * filled_qty
        realized_r = (
            float(result.get("pnl") or 0.0) / risk_usdt
            if risk_usdt > 0
            else 0.0
        )
        active_records = []
        store = getattr(engine, "trading_state_store", None)
        if (
            store is None
            and hasattr(engine, "crypto_entry_lock_reason")
            and callable(ensure_trading_runtime)
        ):
            ensure_trading_runtime(engine)
            store = getattr(engine, "trading_state_store", None)
        if store is not None:
            active_records = store.active_for_symbol(accounting_symbol)
            if not active_records:
                historic_records = store.records_for_symbol(accounting_symbol)
                entry_records = [
                    record
                    for record in historic_records
                    if str(
                        getattr(record, "order_intent", "") or ""
                    ).upper()
                    == "ENTRY"
                ]
                active_records = list(reversed(entry_records[-10:]))
        entry_record = active_records[0] if active_records else None
        entry_metadata = dict(
            getattr(entry_record, "metadata", {}) or {}
        )
        primary_strategy = str(
            entry_metadata.get("primary_strategy")
            or getattr(entry_record, "strategy", None)
            or state_data.get("strategy")
            or "UNKNOWN"
        )
        confirmation_strategies = (
            entry_metadata.get("confirmation_strategies")
            or [primary_strategy]
        )
        confirmation_strategies = list(
            dict.fromkeys(
                str(value)
                for value in confirmation_strategies
                if str(value).strip()
            )
        )
        aggregate_strategy = entry_metadata.get("aggregate_strategy")
        exit_timestamp_ms = max(
            (
                int(float(item.get("timestamp") or 0))
                for item in exit_legs
                if item.get("timestamp") not in (None, "")
            ),
            default=0,
        )
        resolved_exit_time = (
            datetime.fromtimestamp(
                exit_timestamp_ms / 1000.0,
                tz=timezone.utc,
            ).isoformat()
            if exit_timestamp_ms > 0
            else datetime.now(timezone.utc).isoformat()
        )
        trade_key = "|".join(
            (
                accounting_symbol,
                str((open_trade or {}).get("side") or ""),
                str((open_trade or {}).get("entry_time") or ""),
            )
        )
        live_trade = {
            "trade_id": hashlib.sha256(
                trade_key.encode("utf-8")
            ).hexdigest()[:24],
            "client_order_id": getattr(
                entry_record,
                "client_order_id",
                None,
            ),
            "entry_order_id": getattr(
                entry_record,
                "exchange_order_id",
                None,
            ),
            "entry_fill_lookup_start": getattr(
                entry_record,
                "created_at",
                None,
            ),
            "strategy": primary_strategy,
            "primary_strategy": primary_strategy,
            "selected_strategy": primary_strategy,
            "confirmation_strategies": confirmation_strategies,
            "aggregate_strategy": aggregate_strategy,
            "engine": aggregate_strategy
            or state_data.get("engine")
            or getattr(entry_record, "strategy", None),
            "symbol": accounting_symbol,
            "side": (open_trade or {}).get("side"),
            "entry_time": (open_trade or {}).get("entry_time"),
            "exit_time": resolved_exit_time,
            "entry_price": (open_trade or {}).get("entry_price"),
            "exit_price": result.get("exit_price"),
            "pnl_pct": result.get("pnl_pct"),
            "requested_qty": getattr(
                entry_record,
                "requested_qty",
                filled_qty,
            ),
            "filled_qty": filled_qty,
            "leverage": entry_metadata.get("leverage"),
            "gross_pnl_usdt": result.get("pnl"),
            "entry_fee_usdt": None,
            "exit_fee_usdt": None,
            "funding_usdt": None,
            "slippage_usdt": None,
            "net_pnl_usdt": result.get("pnl"),
            "r_multiple": realized_r,
            "realized_r": realized_r,
            "mfe_r": state_data.get("mfe_r"),
            "mae_r": state_data.get("mae_r"),
            "exit_reason": close_reason,
            "exit_legs": exit_legs,
            "entry_plan_summary": entry_metadata.get("entry_plan_summary")
            or {},
            "accounting_source": result.get("source"),
            "provisional": True,
        }
        if store is not None and callable(persist_live_trade):
            persist_live_trade(live_trade, store=store)
        elif store is None:
            logger.warning(
                "Live trade persistence unavailable for %s outside initialized runtime",
                accounting_symbol,
            )
    except Exception:
        logger.exception(
            "Persistent live trade result recording failed for %s",
            accounting_symbol,
        )
    return result


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
        entry_time = trade.get("entry_fill_lookup_start") or trade.get("entry_time")
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
            # The DB entry timestamp is written after exchange confirmation and
            # protection setup. Fetch slightly before it so the entry fill (and
            # therefore its fee) is not silently omitted.
            fetch_since = max(0, since - 30_000) if since is not None else None
            fills = await asyncio.to_thread(fetch_trades, symbol, fetch_since)
        except Exception as exc:
            return self._persist_pending(trade, f"trade fill lookup failed: {type(exc).__name__}:{exc}")
        entry_side = "buy" if str(trade.get("side") or "").lower() == "long" else "sell"
        related = []
        for item in fills or []:
            if not isinstance(item, dict):
                continue
            raw_item_info = item.get("info")
            item_info = raw_item_info if isinstance(raw_item_info, dict) else {}
            timestamp = int(_number(item.get("timestamp") or item_info.get("time")))
            if fetch_since is not None and timestamp and timestamp < fetch_since:
                continue
            item_side = str(item.get("side") or item_info.get("side") or "").lower()
            if since is not None and timestamp and timestamp < since and item_side != entry_side:
                continue
            if until is not None and timestamp and timestamp > until + 60_000:
                continue
            related.append(item)
        if not related:
            return self._persist_pending(trade, "no exchange fills found")

        entry_fees = 0.0
        exit_fees = 0.0
        entry_order_id = str(trade.get("entry_order_id") or "")
        exit_order_ids = {
            str(item.get("order_id") or "")
            for item in trade.get("exit_legs") or []
            if isinstance(item, dict) and item.get("order_id") not in (None, "")
        }
        for fill in related:
            raw_fee = fill.get("fee")
            raw_info = fill.get("info")
            fee = raw_fee if isinstance(raw_fee, dict) else {}
            info = raw_info if isinstance(raw_info, dict) else {}
            commission = abs(_number(fee.get("cost") or info.get("commission")))
            order_id = str(
                fill.get("order")
                or info.get("orderId")
                or info.get("order")
                or ""
            )
            fill_side = str(fill.get("side") or info.get("side") or "").lower()
            if entry_order_id and order_id == entry_order_id:
                entry_fees += commission
            elif order_id and order_id in exit_order_ids:
                exit_fees += commission
            elif fill_side == entry_side:
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

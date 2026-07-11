"""Startup and reconnect reconciliation between local and exchange state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Awaitable, Callable

from .liquidation_guard import (
    resolve_liquidation_safety_config,
    validate_stop_against_liquidation,
)
from .order_state import (
    ACTIVE_ORDER_STATES,
    OrderRecord,
    OrderState,
    SQLiteTradingStateStore,
    build_client_order_id,
)


logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass
class ReconciliationResult:
    safe_to_trade: bool
    positions: list[dict[str, Any]] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    reconciled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _position_qty(position: dict[str, Any]) -> float:
    info = _as_dict(position.get("info"))
    value = position.get("contracts") or info.get("positionAmt") or 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _position_symbol(position: dict[str, Any]) -> str:
    info = _as_dict(position.get("info"))
    return str(position.get("symbol") or info.get("symbol") or "")


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").upper().replace(":USDT", "").replace(":USDC", "")
    return "".join(character for character in text if character.isalnum())


def _order_client_id(order: dict[str, Any]) -> str:
    info = _as_dict(order.get("info"))
    return str(
        order.get("clientOrderId")
        or order.get("clientAlgoId")
        or info.get("clientOrderId")
        or info.get("clientAlgoId")
        or ""
    )


def _order_symbol(order: dict[str, Any]) -> str:
    info = _as_dict(order.get("info"))
    return str(order.get("symbol") or info.get("symbol") or "")


def _is_reduce_only(order: dict[str, Any]) -> bool:
    info = _as_dict(order.get("info"))
    for value in (order.get("reduceOnly"), info.get("reduceOnly"), info.get("closePosition")):
        if isinstance(value, bool) and value:
            return True
        if str(value or "").strip().lower() in {"true", "1", "yes"}:
            return True
    return False


def _is_stop_order(order: dict[str, Any]) -> bool:
    info = _as_dict(order.get("info"))
    order_type = str(
        order.get("type")
        or order.get("orderType")
        or info.get("type")
        or info.get("orderType")
        or ""
    ).upper()
    return "STOP" in order_type and "TAKE_PROFIT" not in order_type


def _is_take_profit_order(order: dict[str, Any]) -> bool:
    info = _as_dict(order.get("info"))
    order_type = str(order.get("type") or info.get("type") or "").upper()
    client_id = _order_client_id(order).lower()
    return "TAKE_PROFIT" in order_type or order_type == "LIMIT" or "-tp" in client_id


def _order_qty(order: dict[str, Any]) -> float:
    info = _as_dict(order.get("info"))
    value = order.get("amount") or info.get("origQty") or info.get("quantity") or 0.0
    try:
        return abs(float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _order_side(order: dict[str, Any]) -> str:
    info = _as_dict(order.get("info"))
    return str(order.get("side") or info.get("side") or "").lower()


def _order_trigger_price(order: dict[str, Any]) -> float:
    info = _as_dict(order.get("info"))
    value = (
        order.get("triggerPrice")
        or order.get("stopPrice")
        or info.get("triggerPrice")
        or info.get("stopPrice")
        or 0.0
    )
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _order_working_type(order: dict[str, Any]) -> str:
    info = _as_dict(order.get("info"))
    return str(order.get("workingType") or info.get("workingType") or "").upper()


def _position_side(position: dict[str, Any]) -> str:
    side = str(position.get("side") or "").lower()
    if side in {"long", "short"}:
        return side
    return "long" if _position_qty(position) > 0 else "short"


def _position_number(position: dict[str, Any], *keys: str) -> float:
    info = _as_dict(position.get("info"))
    for key in keys:
        value = position.get(key)
        if value in (None, ""):
            value = info.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


def _tick_size(exchange: Any, symbol: str) -> float:
    try:
        market = exchange.market(symbol) if hasattr(exchange, "market") else {}
    except Exception:
        market = {}
    info = _as_dict(market.get("info")) if isinstance(market, dict) else {}
    for item in info.get("filters", []) or []:
        if isinstance(item, dict) and item.get("filterType") == "PRICE_FILTER":
            try:
                tick = float(item.get("tickSize") or 0.0)
                if tick > 0:
                    return tick
            except (TypeError, ValueError):
                pass
    precision = market.get("precision", {}) if isinstance(market, dict) else {}
    value = precision.get("price") if isinstance(precision, dict) else None
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
        if number > 0:
            return number if number < 1 else 10 ** (-int(number))
    except (TypeError, ValueError, OverflowError):
        pass
    return 0.0


async def _fetch_positions(exchange: Any) -> list[dict[str, Any]]:
    if not hasattr(exchange, "fetch_positions"):
        raise RuntimeError("exchange does not support fetch_positions")
    positions = await asyncio.to_thread(exchange.fetch_positions)
    return [dict(position) for position in positions or [] if abs(_position_qty(position)) > 0]


async def _fetch_open_orders(exchange: Any) -> list[dict[str, Any]]:
    if not hasattr(exchange, "fetch_open_orders"):
        raise RuntimeError("exchange does not support fetch_open_orders")
    try:
        orders = await asyncio.to_thread(exchange.fetch_open_orders)
    except Exception:
        if not hasattr(exchange, "markets"):
            raise
        orders = []
        for symbol in getattr(exchange, "markets", {}) or {}:
            try:
                orders.extend(await asyncio.to_thread(exchange.fetch_open_orders, symbol))
            except Exception:
                logger.debug("symbol open-order reconciliation failed: %s", symbol, exc_info=True)
    merged = [dict(order) for order in orders or []]
    fetch_algo = getattr(exchange, "fapiPrivateGetOpenAlgoOrders", None)
    if callable(fetch_algo):
        algo_response = await asyncio.to_thread(fetch_algo, {})
        if isinstance(algo_response, dict):
            algo_orders = (
                algo_response.get("orders")
                or algo_response.get("algoOrders")
                or algo_response.get("rows")
                or algo_response.get("data")
                or []
            )
        else:
            algo_orders = algo_response or []
        for raw in algo_orders:
            if not isinstance(raw, dict):
                continue
            merged.append(
                {
                    "id": raw.get("algoId"),
                    "algoId": raw.get("algoId"),
                    "clientAlgoId": raw.get("clientAlgoId"),
                    "clientOrderId": raw.get("clientAlgoId"),
                    "symbol": raw.get("symbol"),
                    "type": raw.get("orderType"),
                    "orderType": raw.get("orderType"),
                    "side": raw.get("side"),
                    "amount": raw.get("quantity"),
                    "triggerPrice": raw.get("triggerPrice"),
                    "reduceOnly": raw.get("reduceOnly"),
                    "workingType": raw.get("workingType"),
                    "status": raw.get("algoStatus"),
                    "info": dict(raw),
                    "_protection_source": "binance_algo",
                }
            )
    return merged


async def reconcile_exchange_state(
    exchange: Any,
    store: SQLiteTradingStateStore,
    *,
    position_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    open_orders_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    single_position: bool = True,
    liquidation_config: dict[str, Any] | None = None,
) -> ReconciliationResult:
    """Reconcile without placing or canceling orders.

    Existing protection recovery remains responsible for repairs and cleanup.
    This function blocks entries whenever the read-only exchange snapshot is not
    sufficient to prove a safe state.
    """

    issues: list[str] = []
    try:
        positions = await (position_fetcher() if position_fetcher else _fetch_positions(exchange))
    except Exception as exc:
        logger.exception("Startup position reconciliation failed")
        result = ReconciliationResult(False, issues=[f"position_fetch_failed:{type(exc).__name__}"])
        store.set_runtime_state("last_reconciliation", result.__dict__)
        return result
    try:
        open_orders = await (open_orders_fetcher() if open_orders_fetcher else _fetch_open_orders(exchange))
    except Exception as exc:
        logger.exception("Startup open-order reconciliation failed")
        result = ReconciliationResult(
            False,
            positions=positions,
            issues=[f"open_orders_fetch_failed:{type(exc).__name__}"],
        )
        store.set_runtime_state("last_reconciliation", result.__dict__)
        return result

    if single_position and len(positions) > 1:
        issues.append(f"multiple_positions:{len(positions)}")

    open_by_client_id = {_order_client_id(order): order for order in open_orders if _order_client_id(order)}
    active_records = store.list_by_states(ACTIVE_ORDER_STATES)
    active_by_symbol: dict[str, list[OrderRecord]] = {}
    for record in active_records:
        active_by_symbol.setdefault(_normalize_symbol(record.symbol), []).append(record)

    position_symbols = {_normalize_symbol(_position_symbol(position)) for position in positions}
    for position in positions:
        symbol = _position_symbol(position)
        normalized = _normalize_symbol(symbol)
        records = active_by_symbol.get(normalized, [])
        if not records:
            side = str(position.get("side") or ("long" if _position_qty(position) > 0 else "short"))
            signature = position.get("timestamp") or position.get("datetime") or "startup"
            client_id = build_client_order_id("recon", symbol, side, signature, "found")
            store.upsert(
                OrderRecord(
                    client_order_id=client_id,
                    symbol=symbol,
                    side=side.upper(),
                    strategy="EXTERNAL_OR_PRE_RECONCILIATION",
                    signal_timestamp=str(signature),
                    requested_qty=abs(_position_qty(position)),
                    filled_qty=abs(_position_qty(position)),
                    average_fill_price=float(position.get("entryPrice") or 0.0),
                    order_state=OrderState.FILLED_UNPROTECTED.value,
                    metadata={"source": "startup_exchange_reconciliation"},
                )
            )
            issues.append(f"exchange_position_without_local_record:{symbol}")

        symbol_orders = [order for order in open_orders if _normalize_symbol(_order_symbol(order)) == normalized]
        position_qty = abs(_position_qty(position))
        oversized_orders = [
            order for order in symbol_orders
            if _is_reduce_only(order)
            and _order_qty(order) > position_qty + max(position_qty * 0.01, 1e-12)
        ]
        tp_qty = sum(
            _order_qty(order)
            for order in symbol_orders
            if _is_reduce_only(order) and _is_take_profit_order(order)
        )
        if oversized_orders or tp_qty > position_qty + max(position_qty * 0.01, 1e-12):
            issues.append(
                f"reduce_only_qty_exceeds_position:{symbol}:tp={tp_qty}>position={position_qty}"
            )
        side = _position_side(position)
        close_side = "sell" if side == "long" else "buy"
        liquidation_price = _position_number(
            position,
            "liquidationPrice",
            "liquidation_price",
        )
        entry_price = _position_number(position, "entryPrice", "entry_price")
        tick_size = _tick_size(exchange, symbol)
        safety_cfg = resolve_liquidation_safety_config(liquidation_config)
        enforce_liquidation_safety = (
            str(getattr(exchange, "id", "") or "").lower() == "binance"
            or liquidation_price > 0
        )
        valid_stops: list[dict[str, Any]] = []
        stop_failures: list[str] = []
        for stop in [order for order in symbol_orders if _is_stop_order(order)]:
            if not _is_reduce_only(stop):
                stop_failures.append("stop_not_reduce_only")
                continue
            if not enforce_liquidation_safety:
                valid_stops.append(stop)
                continue
            if _order_side(stop) != close_side:
                stop_failures.append("stop_wrong_side")
                continue
            if _order_qty(stop) + max(position_qty * 0.01, 1e-12) < position_qty:
                stop_failures.append("stop_qty_insufficient")
                continue
            if liquidation_price <= 0 or tick_size <= 0:
                stop_failures.append("liquidation_price_unavailable")
                continue
            safety = validate_stop_against_liquidation(
                side,
                _order_trigger_price(stop),
                liquidation_price,
                tick_size,
                safety_cfg.minimum_buffer_pct,
                safety_cfg.minimum_buffer_ticks,
                _order_working_type(stop),
                entry_price,
            )
            if not safety.valid:
                stop_failures.append(safety.reason)
                continue
            valid_stops.append(stop)
        if enforce_liquidation_safety and liquidation_price <= 0:
            issues.append(f"liquidation_price_unavailable:{symbol}")
        if not valid_stops:
            issues.append(f"position_without_verified_stop:{symbol}")
            issues.extend(f"unsafe_stop:{symbol}:{reason}" for reason in sorted(set(stop_failures)))
        else:
            for record in records:
                if record.order_state in {
                    OrderState.FILLED_UNPROTECTED.value,
                    OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
                }:
                    stop = valid_stops[0]
                    info = _as_dict(stop.get("info"))
                    store.transition(
                        record.client_order_id,
                        OrderState.PROTECTED,
                        stop_order_id=str(stop.get("id") or info.get("orderId") or "") or None,
                        last_error=None,
                    )

    for record in active_records:
        normalized = _normalize_symbol(record.symbol)
        if record.order_state == OrderState.SUBMITTED_UNKNOWN.value:
            if record.client_order_id in open_by_client_id:
                store.transition(record.client_order_id, OrderState.ACKNOWLEDGED, last_error=None)
            else:
                issues.append(f"submitted_unknown:{record.client_order_id}")
        if record.order_state in {
            OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
            OrderState.FILLED_LIQUIDATION_CONFLICT.value,
            OrderState.FILLED_UNPROTECTED.value,
            OrderState.PROTECTED.value,
            OrderState.CLOSING.value,
            OrderState.EMERGENCY_CLOSE_FAILED.value,
        } and normalized not in position_symbols:
            completed_emergency_close = next(
                (
                    candidate
                    for candidate in store.records_for_symbol(record.symbol)
                    if candidate.strategy == "EMERGENCY_PROTECTION_CLOSE"
                    and candidate.order_state == OrderState.CLOSED.value
                    and candidate.updated_at >= record.created_at
                ),
                None,
            )
            if (
                record.strategy == "EXTERNAL_OR_PRE_RECONCILIATION"
                and completed_emergency_close is not None
            ):
                store.transition(
                    record.client_order_id,
                    OrderState.CLOSED,
                    last_error=None,
                    reconciled_by_close_client_order_id=(
                        completed_emergency_close.client_order_id
                    ),
                )
                continue
            issues.append(f"local_active_without_exchange_position:{record.client_order_id}")

    for order in open_orders:
        if _is_reduce_only(order) and _normalize_symbol(_order_symbol(order)) not in position_symbols:
            issues.append(f"orphan_reduce_only_order:{_order_symbol(order)}:{_order_client_id(order)}")

    result = ReconciliationResult(
        safe_to_trade=not issues,
        positions=positions,
        open_orders=open_orders,
        issues=issues,
    )
    store.set_runtime_state("last_reconciliation", result.__dict__)
    store.set_runtime_state("entry_lock_reason", None if result.safe_to_trade else "RECONCILIATION_REQUIRED")
    return result

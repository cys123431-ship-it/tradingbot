"""Startup and reconnect reconciliation between local and exchange state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Awaitable, Callable

from .binance_algo_gateway import BinanceAlgoOrderGateway, normalize_futures_market_id
from .liquidation_guard import (
    resolve_liquidation_safety_config,
    validate_stop_against_liquidation,
)
from .order_state import (
    ACTIVE_ORDER_STATES,
    OrderIntent,
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
    snapshot_complete: bool = False
    user_stream_ready: bool = False
    positions_ok: bool = False
    regular_orders_ok: bool = False
    algo_orders_ok: bool = False
    positions: list[dict[str, Any]] = field(default_factory=list)
    regular_orders: list[dict[str, Any]] = field(default_factory=list)
    algo_orders: list[dict[str, Any]] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    unresolved_records: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    reconciled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class ExchangeStateSnapshot:
    positions: tuple[dict[str, Any], ...] = ()
    regular_orders: tuple[dict[str, Any], ...] = ()
    algo_orders: tuple[dict[str, Any], ...] = ()
    positions_ok: bool = False
    regular_orders_ok: bool = False
    algo_orders_ok: bool = False
    errors: tuple[str, ...] = ()
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def complete(self) -> bool:
        return self.positions_ok and self.regular_orders_ok and self.algo_orders_ok


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
    return normalize_futures_market_id(value)


def _exchange_order_status(order: dict[str, Any] | None) -> str:
    payload = _as_dict(order)
    info = _as_dict(payload.get("info"))
    return str(
        payload.get("status")
        or payload.get("algoStatus")
        or info.get("status")
        or info.get("algoStatus")
        or ""
    ).upper()


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
    for value in (
        order.get("reduceOnly"),
        order.get("closePosition"),
        info.get("reduceOnly"),
        info.get("closePosition"),
    ):
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


def _is_close_position_order(order: dict[str, Any]) -> bool:
    info = _as_dict(order.get("info"))
    for value in (order.get("closePosition"), info.get("closePosition")):
        if isinstance(value, bool) and value:
            return True
        if str(value or "").strip().lower() in {"true", "1", "yes"}:
            return True
    return False


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


async def _fetch_exchange_snapshot(
    exchange: Any,
    *,
    position_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    open_orders_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
) -> ExchangeStateSnapshot:
    errors: list[str] = []
    positions: list[dict[str, Any]] = []
    regular_orders: list[dict[str, Any]] = []
    algo_orders: list[dict[str, Any]] = []
    try:
        positions = await (position_fetcher() if position_fetcher else _fetch_positions(exchange))
        positions_ok = True
    except Exception as exc:
        positions_ok = False
        errors.append(f"position_fetch_failed:{type(exc).__name__}:{exc}")

    if open_orders_fetcher is not None:
        try:
            regular_orders = [dict(item) for item in await open_orders_fetcher() or []]
            regular_orders_ok = True
            algo_orders_ok = True
        except Exception as exc:
            regular_orders_ok = False
            algo_orders_ok = False
            errors.append(f"injected_order_snapshot_failed:{type(exc).__name__}:{exc}")
    else:
        fetch_open = getattr(exchange, "fetch_open_orders", None)
        if not callable(fetch_open):
            regular_orders_ok = False
            errors.append("regular_open_orders_endpoint_unavailable")
        else:
            try:
                regular_orders = [
                    dict(item)
                    for item in await asyncio.to_thread(fetch_open) or []
                ]
                regular_orders_ok = True
            except Exception as exc:
                regular_orders_ok = False
                errors.append(f"regular_open_orders_failed:{type(exc).__name__}:{exc}")

        is_binance = str(getattr(exchange, "id", "") or "").lower() in {
            "binance",
            "binanceusdm",
        }
        if is_binance:
            algo_snapshot = await BinanceAlgoOrderGateway(exchange).fetch_open_orders()
            algo_orders_ok = algo_snapshot.ok
            algo_orders = list(algo_snapshot.orders)
            if not algo_snapshot.ok:
                errors.append(algo_snapshot.error or "algo_open_orders_failed")
        else:
            algo_orders_ok = True

    return ExchangeStateSnapshot(
        positions=tuple(positions),
        regular_orders=tuple(regular_orders),
        algo_orders=tuple(algo_orders),
        positions_ok=positions_ok,
        regular_orders_ok=regular_orders_ok,
        algo_orders_ok=algo_orders_ok,
        errors=tuple(errors),
    )


async def _lookup_regular_order(
    exchange: Any,
    record: OrderRecord,
) -> tuple[str, dict[str, Any] | None, str | None]:
    method = getattr(exchange, "fapiPrivateGetOrder", None)
    if not callable(method):
        return "UNKNOWN", None, "regular_order_lookup_endpoint_unavailable"
    market_id = _normalize_symbol(record.symbol)
    try:
        raw = await asyncio.to_thread(
            method,
            {"symbol": market_id, "origClientOrderId": record.client_order_id},
        )
    except Exception as exc:
        text = str(exc).lower()
        if any(token in text for token in ("unknown order", "does not exist", "-2013")):
            return "NOT_FOUND", None, None
        return "UNKNOWN", None, f"{type(exc).__name__}:{exc}"
    if not isinstance(raw, dict):
        return "UNKNOWN", None, "invalid_regular_order_response"
    return "FOUND", raw, None


async def reconcile_exchange_state(
    exchange: Any,
    store: SQLiteTradingStateStore,
    *,
    position_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    open_orders_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    single_position: bool = True,
    liquidation_config: dict[str, Any] | None = None,
    user_stream_ready: bool = True,
    require_user_stream: bool = False,
) -> ReconciliationResult:
    """Reconcile without placing or canceling orders.

    Existing protection recovery remains responsible for repairs and cleanup.
    This function blocks entries whenever the read-only exchange snapshot is not
    sufficient to prove a safe state.
    """

    snapshot = await _fetch_exchange_snapshot(
        exchange,
        position_fetcher=position_fetcher,
        open_orders_fetcher=open_orders_fetcher,
    )
    issues: list[str] = list(snapshot.errors)
    positions = list(snapshot.positions)
    regular_orders = list(snapshot.regular_orders)
    algo_orders = list(snapshot.algo_orders)
    open_orders = regular_orders + algo_orders
    unresolved_records: list[str] = []
    if require_user_stream and not user_stream_ready:
        issues.append("user_stream_not_ready")

    if single_position and len(positions) > 1:
        issues.append(f"multiple_positions:{len(positions)}")

    open_by_client_id = {_order_client_id(order): order for order in open_orders if _order_client_id(order)}
    active_records = store.list_by_states(ACTIVE_ORDER_STATES)
    strict_individual_lookup = (
        open_orders_fetcher is None
        and str(getattr(exchange, "id", "") or "").lower() in {"binance", "binanceusdm"}
    )
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
            if (
                not _is_close_position_order(stop)
                and _order_qty(stop) + max(position_qty * 0.01, 1e-12) < position_qty
            ):
                stop_failures.append("stop_qty_insufficient")
                continue
            if liquidation_price <= 0 or tick_size <= 0:
                stop_failures.append("liquidation_price_unavailable")
                continue
            working_type = _order_working_type(stop) or "CONTRACT_PRICE"
            safety = validate_stop_against_liquidation(
                side,
                _order_trigger_price(stop),
                liquidation_price,
                tick_size,
                safety_cfg.minimum_buffer_pct,
                safety_cfg.minimum_buffer_ticks,
                working_type,
                entry_price,
                accepted_working_types={"MARK_PRICE", "CONTRACT_PRICE"},
                non_mark_minimum_buffer_pct="0.05",
                non_mark_buffer_multiplier="2",
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
        position_present = normalized in position_symbols
        lookup_order: dict[str, Any] | None = None
        synthetic_position_record = (
            record.strategy == "EXTERNAL_OR_PRE_RECONCILIATION"
            and record.metadata.get("source")
            == "startup_exchange_reconciliation"
        )
        lookup_required_states = {
            OrderState.PLANNED.value,
            OrderState.SUBMITTING.value,
            OrderState.SUBMITTED_UNKNOWN.value,
            OrderState.ACKNOWLEDGED.value,
            OrderState.PARTIALLY_FILLED.value,
            OrderState.CLOSING.value,
            OrderState.PARTIALLY_CLOSED.value,
        }
        if strict_individual_lookup and not (
            synthetic_position_record and position_present
        ):
            lookup_required_states.update(ACTIVE_ORDER_STATES)
        if (
            record.order_state in lookup_required_states
            and record.client_order_id not in open_by_client_id
        ):
            try:
                intent = OrderIntent(record.order_intent)
            except ValueError:
                unresolved_records.append(f"invalid_intent:{record.client_order_id}")
                intent = OrderIntent.ENTRY
            if open_orders_fetcher is not None:
                lookup_status, lookup_error = "NOT_FOUND", None
            elif intent in {OrderIntent.PROTECTION_SL, OrderIntent.PROTECTION_TP}:
                lookup = await BinanceAlgoOrderGateway(exchange).fetch_by_client_id(
                    record.client_order_id
                )
                lookup_status, lookup_error = lookup.status.value, lookup.error
                lookup_order = lookup.order
            else:
                lookup_status, lookup_order, lookup_error = await _lookup_regular_order(exchange, record)
            if lookup_status == "UNKNOWN":
                unresolved_records.append(
                    f"order_lookup_unknown:{record.client_order_id}:{lookup_error}"
                )
            elif lookup_status == "NOT_FOUND":
                close_intents = {
                    OrderIntent.CLOSE,
                    OrderIntent.EMERGENCY_CLOSE,
                    OrderIntent.MANUAL_CLOSE,
                    OrderIntent.GRID_EXIT,
                }
                if intent in close_intents and not position_present:
                    store.transition(record.client_order_id, OrderState.CLOSED, last_error=None)
                elif intent not in close_intents and not position_present:
                    store.transition(record.client_order_id, OrderState.FAILED, last_error="order not found")
                else:
                    unresolved_records.append(f"order_not_found:{record.client_order_id}")
            record = store.get(record.client_order_id) or record
        position_bound_states = {
            OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
            OrderState.FILLED_LIQUIDATION_CONFLICT.value,
            OrderState.FILLED_UNPROTECTED.value,
            OrderState.PROTECTED.value,
            OrderState.CLOSING.value,
            OrderState.PARTIALLY_CLOSED.value,
            OrderState.EMERGENCY_CLOSE_FAILED.value,
        }
        if (
            not position_present
            and record.order_state in position_bound_states
            and _exchange_order_status(lookup_order)
            in {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
        ):
            store.transition(
                record.client_order_id,
                OrderState.CLOSED,
                last_error=None,
                reconciled_without_exchange_position=True,
                reconciled_terminal_order_status=_exchange_order_status(lookup_order),
            )
            continue
        if record.order_state == OrderState.SUBMITTED_UNKNOWN.value:
            if record.client_order_id in open_by_client_id:
                store.transition(record.client_order_id, OrderState.ACKNOWLEDGED, last_error=None)
            else:
                unresolved_records.append(f"submitted_unknown:{record.client_order_id}")
        if record.order_state in {
            OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
            OrderState.FILLED_LIQUIDATION_CONFLICT.value,
            OrderState.FILLED_UNPROTECTED.value,
            OrderState.PROTECTED.value,
            OrderState.CLOSING.value,
            OrderState.PARTIALLY_CLOSED.value,
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
        safe_to_trade=(
            snapshot.complete
            and (user_stream_ready or not require_user_stream)
            and not unresolved_records
            and not issues
        ),
        snapshot_complete=snapshot.complete,
        user_stream_ready=bool(user_stream_ready),
        positions_ok=snapshot.positions_ok,
        regular_orders_ok=snapshot.regular_orders_ok,
        algo_orders_ok=snapshot.algo_orders_ok,
        positions=positions,
        regular_orders=regular_orders,
        algo_orders=algo_orders,
        open_orders=open_orders,
        unresolved_records=unresolved_records,
        issues=issues,
    )
    store.set_runtime_state("last_reconciliation", result.__dict__)
    store.set_runtime_state("entry_lock_reason", None if result.safe_to_trade else "RECONCILIATION_REQUIRED")
    return result

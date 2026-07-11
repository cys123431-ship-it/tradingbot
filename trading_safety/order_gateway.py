"""Idempotent exchange submission and ambiguous-response recovery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
from typing import Any, Awaitable, Callable

from .order_state import (
    OrderRecord,
    OrderState,
    SQLiteTradingStateStore,
    build_client_order_id,
)


logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class OrderSubmissionResult:
    client_order_id: str
    state: str
    order: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    recovered: bool = False
    error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.state in {
            OrderState.ACKNOWLEDGED.value,
            OrderState.PARTIALLY_FILLED.value,
            OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
            OrderState.FILLED_UNPROTECTED.value,
            OrderState.PROTECTED.value,
        }


def _order_client_id(order: dict[str, Any] | None) -> str:
    if not isinstance(order, dict):
        return ""
    info = _as_dict(order.get("info"))
    return str(
        order.get("clientOrderId")
        or order.get("client_order_id")
        or info.get("clientOrderId")
        or info.get("origClientOrderId")
        or ""
    )


def _order_id(order: dict[str, Any] | None) -> str | None:
    if not isinstance(order, dict):
        return None
    info = _as_dict(order.get("info"))
    value = order.get("id") or order.get("orderId") or info.get("orderId")
    return str(value) if value is not None else None


def _order_fill(order: dict[str, Any] | None) -> tuple[float, float]:
    if not isinstance(order, dict):
        return 0.0, 0.0
    info = _as_dict(order.get("info"))
    filled = order.get("filled") or info.get("executedQty") or 0.0
    average = order.get("average") or info.get("avgPrice") or order.get("price") or 0.0
    try:
        return abs(float(filled or 0.0)), float(average or 0.0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def _classify_order(order: dict[str, Any] | None) -> str:
    if not isinstance(order, dict):
        return OrderState.ACKNOWLEDGED.value
    info = _as_dict(order.get("info"))
    status = str(order.get("status") or info.get("status") or "").upper()
    filled, _ = _order_fill(order)
    amount = order.get("amount") or info.get("origQty") or 0.0
    try:
        requested = abs(float(amount or 0.0))
    except (TypeError, ValueError):
        requested = 0.0
    if status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
        return OrderState.CANCELED.value if status not in {"REJECTED"} else OrderState.FAILED.value
    if status in {"CLOSED", "FILLED"} or (filled > 0 and requested > 0 and filled + 1e-12 >= requested):
        return OrderState.FILLED_UNVERIFIED_LIQUIDATION.value
    if status in {"PARTIALLY_FILLED", "PARTIALLYFILLED"} or filled > 0:
        return OrderState.PARTIALLY_FILLED.value
    return OrderState.ACKNOWLEDGED.value


def _is_definitive_rejection(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return (
        any(token in name for token in ("invalidorder", "insufficientfunds", "authentication", "permissiondenied"))
        or "rejected" in text
    )


def _is_authentication_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return "authentication" in name or "permissiondenied" in name


class IdempotentOrderGateway:
    def __init__(
        self,
        exchange: Any,
        store: SQLiteTradingStateStore,
        *,
        position_fetcher: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        recovery_delays: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0),
        partial_fill_policy: str = "cancel_remainder",
    ) -> None:
        self.exchange = exchange
        self.store = store
        self.position_fetcher = position_fetcher
        self.recovery_delays = recovery_delays
        self.partial_fill_policy = str(partial_fill_policy or "cancel_remainder").lower()
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    def _lock(self, symbol: str) -> asyncio.Lock:
        key = str(symbol or "").upper()
        if key not in self._symbol_locks:
            self._symbol_locks[key] = asyncio.Lock()
        return self._symbol_locks[key]

    async def _fetch_position(self, symbol: str) -> dict[str, Any] | None:
        if self.position_fetcher is not None:
            return await self.position_fetcher(symbol)
        if not hasattr(self.exchange, "fetch_positions"):
            return None
        positions = await asyncio.to_thread(self.exchange.fetch_positions, [symbol])
        for position in positions or []:
            contracts = position.get("contracts") or position.get("info", {}).get("positionAmt") or 0.0
            try:
                if abs(float(contracts or 0.0)) > 0:
                    return position
            except (TypeError, ValueError):
                continue
        return None

    async def _fetch_order_by_client_id(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        market_id = str(symbol).replace("/", "").split(":", 1)[0]
        lookup_errors: list[BaseException] = []
        raw_methods = (
            "fapiPrivateGetOrder",
            "fapiPrivateGetOrderV2",
            "fapiPrivateGetOrderV3",
        )
        for method_name in raw_methods:
            method = getattr(self.exchange, method_name, None)
            if method is None:
                continue
            try:
                return await asyncio.to_thread(
                    method,
                    {"symbol": market_id, "origClientOrderId": client_order_id},
                )
            except Exception as exc:
                text = str(exc).lower()
                if any(token in text for token in ("unknown order", "order does not exist", "-2013")):
                    return None
                lookup_errors.append(exc)
                logger.debug("client order raw lookup failed: %s", exc, exc_info=True)
        fetch_order = getattr(self.exchange, "fetch_order", None)
        if fetch_order is not None:
            try:
                return await asyncio.to_thread(
                    fetch_order,
                    client_order_id,
                    symbol,
                    {"origClientOrderId": client_order_id},
                )
            except Exception as exc:
                text = str(exc).lower()
                if any(token in text for token in ("unknown order", "order does not exist", "-2013")):
                    return None
                lookup_errors.append(exc)
                logger.debug("client order generic lookup failed: %s", exc, exc_info=True)
        fetch_open_orders = getattr(self.exchange, "fetch_open_orders", None)
        if fetch_open_orders is not None:
            try:
                orders = await asyncio.to_thread(fetch_open_orders, symbol)
                return next((order for order in orders or [] if _order_client_id(order) == client_order_id), None)
            except Exception as exc:
                lookup_errors.append(exc)
                logger.debug("open-order fallback lookup failed", exc_info=True)
        if lookup_errors:
            raise RuntimeError(
                f"client order lookup unavailable: {type(lookup_errors[-1]).__name__}: {lookup_errors[-1]}"
            )
        return None

    def _transition_from_exchange_order(
        self,
        client_order_id: str,
        order: dict[str, Any],
    ) -> OrderRecord:
        state = _classify_order(order)
        filled, average = _order_fill(order)
        return self.store.transition(
            client_order_id,
            state,
            exchange_order_id=_order_id(order),
            filled_qty=filled,
            average_fill_price=average,
            last_error=None,
        )

    async def _cancel_partial_remainder(self, symbol: str, order: dict[str, Any]) -> None:
        if self.partial_fill_policy != "cancel_remainder":
            return
        order_id = _order_id(order)
        cancel_order = getattr(self.exchange, "cancel_order", None)
        if not order_id or cancel_order is None:
            return
        try:
            await asyncio.to_thread(cancel_order, order_id, symbol)
        except Exception:
            logger.exception("Partial-fill remainder cancellation failed: %s %s", symbol, order_id)
            raise

    async def _create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        qty: float,
        price: float | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        method = self.exchange.create_order
        try:
            supports_params = "params" in inspect.signature(method).parameters
        except (TypeError, ValueError):
            supports_params = True
        if supports_params:
            return await asyncio.to_thread(
                method,
                symbol,
                order_type,
                side,
                qty,
                price,
                params,
            )
        if str(getattr(self.exchange, "id", "")).lower() in {"binance", "binanceusdm"}:
            raise RuntimeError("Binance create_order adapter does not accept params/newClientOrderId")
        return await asyncio.to_thread(method, symbol, order_type, side, qty)

    async def _recover(
        self,
        record: OrderRecord,
        *,
        wait: bool,
    ) -> OrderSubmissionResult | None:
        attempts = self.recovery_delays if wait else (0.0,)
        for delay in attempts:
            if delay > 0:
                await asyncio.sleep(delay)
            current = self.store.get(record.client_order_id)
            if current and current.order_state not in {
                OrderState.SUBMITTING.value,
                OrderState.SUBMITTED_UNKNOWN.value,
                OrderState.ACKNOWLEDGED.value,
                OrderState.PARTIALLY_FILLED.value,
            }:
                return OrderSubmissionResult(
                    record.client_order_id,
                    current.order_state,
                    recovered=True,
                    error=current.last_error,
                )
            try:
                order = await self._fetch_order_by_client_id(record.symbol, record.client_order_id)
            except Exception as exc:
                current = self.store.get(record.client_order_id)
                retry_count = int(getattr(current, "retry_count", 0) or 0) + 1
                self.store.transition(
                    record.client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error=f"recovery order lookup failed: {exc}",
                    retry_count=retry_count,
                )
                continue
            if order:
                updated = self._transition_from_exchange_order(record.client_order_id, order)
                try:
                    position = await self._fetch_position(record.symbol)
                except Exception:
                    logger.exception("Position lookup failed while recovering accepted order")
                    position = None
                if position and updated.order_state in {
                    OrderState.ACKNOWLEDGED.value,
                    OrderState.PARTIALLY_FILLED.value,
                }:
                    updated = self.store.transition(
                        record.client_order_id,
                        OrderState.FILLED_UNVERIFIED_LIQUIDATION,
                        filled_qty=max(updated.filled_qty, abs(float(position.get("contracts") or 0.0))),
                        average_fill_price=updated.average_fill_price or float(position.get("entryPrice") or 0.0),
                    )
                if updated.order_state == OrderState.PARTIALLY_FILLED.value:
                    await self._cancel_partial_remainder(record.symbol, order)
                return OrderSubmissionResult(
                    record.client_order_id,
                    updated.order_state,
                    order=order,
                    position=position,
                    recovered=True,
                )
            try:
                position = await self._fetch_position(record.symbol)
            except Exception as exc:
                current = self.store.get(record.client_order_id)
                retry_count = int(getattr(current, "retry_count", 0) or 0) + 1
                self.store.transition(
                    record.client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error=f"recovery position lookup failed: {exc}",
                    retry_count=retry_count,
                )
                continue
            if position:
                contracts = abs(float(position.get("contracts") or position.get("info", {}).get("positionAmt") or 0.0))
                entry = float(position.get("entryPrice") or position.get("info", {}).get("entryPrice") or 0.0)
                updated = self.store.transition(
                    record.client_order_id,
                    OrderState.FILLED_UNVERIFIED_LIQUIDATION,
                    filled_qty=contracts,
                    average_fill_price=entry,
                )
                return OrderSubmissionResult(
                    record.client_order_id,
                    updated.order_state,
                    position=position,
                    recovered=True,
                )
        return None

    async def submit_entry(
        self,
        *,
        strategy: str,
        symbol: str,
        side: str,
        signal_timestamp: Any,
        qty: float,
        params: dict[str, Any] | None = None,
    ) -> OrderSubmissionResult:
        client_order_id = build_client_order_id(strategy, symbol, side, signal_timestamp, "entry")
        async with self._lock(symbol):
            existing = self.store.get(client_order_id)
            if existing is not None:
                if existing.order_state in {
                    OrderState.PROTECTED.value,
                    OrderState.CLOSED.value,
                    OrderState.CANCELED.value,
                    OrderState.FAILED.value,
                }:
                    return OrderSubmissionResult(
                        client_order_id,
                        existing.order_state,
                        recovered=True,
                        error="signal already handled",
                    )
                recovered = await self._recover(existing, wait=False)
                if recovered:
                    return recovered
                return OrderSubmissionResult(
                    client_order_id,
                    existing.order_state,
                    recovered=True,
                    error=existing.last_error,
                )

            blocker = self.store.entry_block_reason(symbol)
            if blocker:
                return OrderSubmissionResult(client_order_id, "BLOCKED", error=blocker)

            try:
                position = await self._fetch_position(symbol)
            except Exception as exc:
                self.store.set_runtime_state(
                    "entry_lock_reason",
                    f"RECONCILIATION_REQUIRED:position_fetch:{type(exc).__name__}",
                )
                return OrderSubmissionResult(
                    client_order_id,
                    "BLOCKED",
                    error=f"position preflight unavailable: {exc}",
                )
            if position:
                return OrderSubmissionResult(client_order_id, "BLOCKED", position=position, error="position already exists")

            record = OrderRecord(
                client_order_id=client_order_id,
                symbol=symbol,
                side=str(side).upper(),
                strategy=strategy,
                signal_timestamp=str(signal_timestamp),
                requested_qty=float(qty),
            )
            self.store.upsert(record)

            try:
                exchange_existing = await self._fetch_order_by_client_id(symbol, client_order_id)
            except Exception as exc:
                self.store.transition(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error=f"pre-submit duplicate lookup unavailable: {exc}",
                )
                self.store.set_runtime_state("entry_lock_reason", "RECONCILIATION_REQUIRED")
                return OrderSubmissionResult(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN.value,
                    error=str(exc),
                )
            if exchange_existing:
                updated = self._transition_from_exchange_order(client_order_id, exchange_existing)
                return OrderSubmissionResult(
                    client_order_id,
                    updated.order_state,
                    order=exchange_existing,
                    recovered=True,
                )

            self.store.transition(client_order_id, OrderState.SUBMITTING)
            order_params = dict(params or {})
            order_params["newClientOrderId"] = client_order_id
            exchange_side = "buy" if str(side).lower() in {"long", "buy"} else "sell"
            try:
                order = await self._create_order(
                    symbol,
                    "market",
                    exchange_side,
                    qty,
                    None,
                    order_params,
                )
            except Exception as exc:
                if _is_definitive_rejection(exc):
                    self.store.transition(client_order_id, OrderState.FAILED, last_error=str(exc))
                    if _is_authentication_error(exc):
                        self.store.set_runtime_state(
                            "entry_lock_reason",
                            f"AUTHENTICATION_ERROR:{type(exc).__name__}",
                        )
                    return OrderSubmissionResult(client_order_id, OrderState.FAILED.value, error=str(exc))
                self.store.transition(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error=f"{type(exc).__name__}: {exc}",
                    retry_count=1,
                )
                unknown_record = self.store.get(client_order_id)
                if unknown_record is None:
                    raise RuntimeError("submitted order state disappeared before recovery")
                recovered = await self._recover(unknown_record, wait=True)
                if recovered:
                    return recovered
                return OrderSubmissionResult(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN.value,
                    error=str(exc),
                )

            updated = self._transition_from_exchange_order(client_order_id, order)
            if updated.order_state == OrderState.PARTIALLY_FILLED.value:
                try:
                    await self._cancel_partial_remainder(symbol, order)
                except Exception as exc:
                    self.store.transition(
                        client_order_id,
                        OrderState.SUBMITTED_UNKNOWN,
                        last_error=f"partial remainder cancel failed: {exc}",
                    )
                    return OrderSubmissionResult(
                        client_order_id,
                        OrderState.SUBMITTED_UNKNOWN.value,
                        order=order,
                        error=str(exc),
                    )
            position = await self._fetch_position(symbol)
            if position:
                position_side = str(position.get("side") or "").lower()
                expected_side = "long" if str(side).lower() in {"long", "buy"} else "short"
                if position_side and position_side != expected_side:
                    message = f"order/position side contradiction: expected={expected_side} actual={position_side}"
                    self.store.transition(
                        client_order_id,
                        OrderState.SUBMITTED_UNKNOWN,
                        last_error=message,
                    )
                    return OrderSubmissionResult(
                        client_order_id,
                        OrderState.SUBMITTED_UNKNOWN.value,
                        order=order,
                        position=position,
                        error=message,
                    )
                filled = abs(float(position.get("contracts") or position.get("info", {}).get("positionAmt") or updated.filled_qty or 0.0))
                average = float(position.get("entryPrice") or position.get("info", {}).get("entryPrice") or updated.average_fill_price or 0.0)
                updated = self.store.transition(
                    client_order_id,
                    OrderState.FILLED_UNVERIFIED_LIQUIDATION,
                    filled_qty=filled,
                    average_fill_price=average,
                )
            return OrderSubmissionResult(
                client_order_id,
                updated.order_state,
                order=order,
                position=position,
            )

    async def submit_position_add(
        self,
        *,
        strategy: str,
        symbol: str,
        side: str,
        signal_timestamp: Any,
        qty: float,
        stage: str,
        params: dict[str, Any] | None = None,
    ) -> OrderSubmissionResult:
        """Idempotently add to an existing same-side position."""

        client_order_id = build_client_order_id(
            strategy,
            symbol,
            side,
            signal_timestamp,
            f"add-{stage}",
        )
        async with self._lock(symbol):
            existing = self.store.get(client_order_id)
            if existing is not None:
                try:
                    order = await self._fetch_order_by_client_id(symbol, client_order_id)
                except Exception as exc:
                    self.store.transition(
                        client_order_id,
                        OrderState.SUBMITTED_UNKNOWN,
                        last_error=f"position-add recovery lookup failed: {exc}",
                    )
                    return OrderSubmissionResult(
                        client_order_id,
                        OrderState.SUBMITTED_UNKNOWN.value,
                        recovered=True,
                        error=str(exc),
                    )
                if order:
                    updated = self._transition_from_exchange_order(client_order_id, order)
                    return OrderSubmissionResult(
                        client_order_id,
                        updated.order_state,
                        order=order,
                        recovered=True,
                    )
                return OrderSubmissionResult(
                    client_order_id,
                    existing.order_state,
                    recovered=True,
                    error="position-add signal already handled",
                )

            blocker = self.store.entry_block_reason(symbol)
            if blocker:
                return OrderSubmissionResult(client_order_id, "BLOCKED", error=blocker)
            try:
                before = await self._fetch_position(symbol)
            except Exception as exc:
                self.store.set_runtime_state(
                    "entry_lock_reason",
                    f"RECONCILIATION_REQUIRED:position_add_preflight:{type(exc).__name__}",
                )
                return OrderSubmissionResult(client_order_id, "BLOCKED", error=str(exc))
            expected_side = "long" if str(side).lower() in {"long", "buy"} else "short"
            if not before or str(before.get("side") or "").lower() != expected_side:
                return OrderSubmissionResult(
                    client_order_id,
                    "BLOCKED",
                    position=before,
                    error="same-side position required for position add",
                )
            before_qty = abs(
                float(before.get("contracts") or before.get("info", {}).get("positionAmt") or 0.0)
            )
            record = OrderRecord(
                client_order_id=client_order_id,
                symbol=symbol,
                side=str(side).upper(),
                strategy=strategy,
                signal_timestamp=str(signal_timestamp),
                requested_qty=float(qty),
                metadata={"position_qty_before": before_qty, "position_add_stage": str(stage)},
            )
            self.store.upsert(record)
            try:
                exchange_existing = await self._fetch_order_by_client_id(symbol, client_order_id)
            except Exception as exc:
                self.store.transition(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error=f"position-add duplicate lookup unavailable: {exc}",
                )
                return OrderSubmissionResult(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN.value,
                    error=str(exc),
                )
            if exchange_existing:
                updated = self._transition_from_exchange_order(client_order_id, exchange_existing)
                return OrderSubmissionResult(
                    client_order_id,
                    updated.order_state,
                    order=exchange_existing,
                    recovered=True,
                )

            self.store.transition(client_order_id, OrderState.SUBMITTING)
            order_params = dict(params or {})
            order_params["newClientOrderId"] = client_order_id
            exchange_side = "buy" if expected_side == "long" else "sell"
            try:
                order = await self._create_order(
                    symbol,
                    "market",
                    exchange_side,
                    qty,
                    None,
                    order_params,
                )
            except Exception as exc:
                self.store.transition(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error=f"{type(exc).__name__}: {exc}",
                    retry_count=1,
                )
                for delay in self.recovery_delays:
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        recovered_order = await self._fetch_order_by_client_id(symbol, client_order_id)
                        after = await self._fetch_position(symbol)
                    except Exception as recovery_exc:
                        current = self.store.get(client_order_id)
                        self.store.transition(
                            client_order_id,
                            OrderState.SUBMITTED_UNKNOWN,
                            last_error=f"position-add recovery failed: {recovery_exc}",
                            retry_count=int(getattr(current, "retry_count", 0) or 0) + 1,
                        )
                        logger.warning(
                            "Position-add recovery attempt failed for %s clientOrderId=%s: %s",
                            symbol,
                            client_order_id,
                            recovery_exc,
                        )
                        continue
                    after_qty = abs(
                        float(
                            (after or {}).get("contracts")
                            or (after or {}).get("info", {}).get("positionAmt")
                            or 0.0
                        )
                    )
                    if recovered_order or after_qty > before_qty + 1e-12:
                        filled_add = max(0.0, after_qty - before_qty)
                        updated = self.store.transition(
                            client_order_id,
                            OrderState.FILLED_UNVERIFIED_LIQUIDATION,
                            exchange_order_id=_order_id(recovered_order),
                            filled_qty=filled_add,
                            average_fill_price=float((after or {}).get("entryPrice") or 0.0),
                            last_error=None,
                        )
                        return OrderSubmissionResult(
                            client_order_id,
                            updated.order_state,
                            order=recovered_order,
                            position=after,
                            recovered=True,
                        )
                return OrderSubmissionResult(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN.value,
                    error=str(exc),
                )

            updated = self._transition_from_exchange_order(client_order_id, order)
            after = await self._fetch_position(symbol)
            after_qty = abs(
                float(
                    (after or {}).get("contracts")
                    or (after or {}).get("info", {}).get("positionAmt")
                    or 0.0
                )
            )
            if after_qty > before_qty + 1e-12:
                updated = self.store.transition(
                    client_order_id,
                    OrderState.FILLED_UNVERIFIED_LIQUIDATION,
                    filled_qty=after_qty - before_qty,
                    average_fill_price=float((after or {}).get("entryPrice") or 0.0),
                )
            return OrderSubmissionResult(
                client_order_id,
                updated.order_state,
                order=order,
                position=after,
            )

    async def submit_reduce_only_close(
        self,
        *,
        strategy: str,
        symbol: str,
        position_side: str,
        position_signature: Any,
        qty: float,
        reason: str,
        params: dict[str, Any] | None = None,
    ) -> OrderSubmissionResult:
        stage = "close" + str(reason or "exit")[:8]
        client_order_id = build_client_order_id(
            strategy,
            symbol,
            position_side,
            position_signature,
            stage,
        )
        async with self._lock(symbol):
            existing = self.store.get(client_order_id)
            if existing:
                recovered = await self._recover(existing, wait=False)
                return recovered or OrderSubmissionResult(
                    client_order_id,
                    existing.order_state,
                    recovered=True,
                    error=existing.last_error,
                )
            record = OrderRecord(
                client_order_id=client_order_id,
                symbol=symbol,
                side=str(position_side).upper(),
                strategy=strategy,
                signal_timestamp=str(position_signature),
                requested_qty=float(qty),
                order_state=OrderState.CLOSING.value,
                metadata={"reason": reason, "reduce_only": True},
            )
            self.store.upsert(record)
            close_params = dict(params or {})
            close_params["reduceOnly"] = True
            close_params["newClientOrderId"] = client_order_id
            close_side = "sell" if str(position_side).lower() == "long" else "buy"
            try:
                order = await self._create_order(
                    symbol,
                    "market",
                    close_side,
                    qty,
                    None,
                    close_params,
                )
            except Exception as exc:
                self.store.transition(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                unknown_record = self.store.get(client_order_id)
                if unknown_record is None:
                    raise RuntimeError("close order state disappeared before recovery")
                recovered = await self._recover(unknown_record, wait=True)
                return recovered or OrderSubmissionResult(
                    client_order_id,
                    OrderState.SUBMITTED_UNKNOWN.value,
                    error=str(exc),
                )
            self._transition_from_exchange_order(client_order_id, order)
            remaining = await self._fetch_position(symbol)
            if not remaining:
                self.store.transition(client_order_id, OrderState.CLOSED)
                return OrderSubmissionResult(client_order_id, OrderState.CLOSED.value, order=order)
            return OrderSubmissionResult(client_order_id, OrderState.CLOSING.value, order=order, position=remaining)

"""Binance USD-M user-data stream with REST reconciliation on reconnect."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
from typing import Any, Awaitable, Callable

from .order_state import (
    ACTIVE_ORDER_STATES,
    OrderIntent,
    OrderState,
    SQLiteTradingStateStore,
)


logger = logging.getLogger(__name__)

TERMINAL_EXCHANGE_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
STATUS_RANK = {
    "NEW": 0,
    "ACKNOWLEDGED": 0,
    "PARTIALLY_FILLED": 1,
    "FILLED": 2,
    "CANCELED": 2,
    "CANCELLED": 2,
    "EXPIRED": 2,
    "REJECTED": 2,
}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


class BinanceUserDataStream:
    def __init__(
        self,
        exchange: Any,
        store: SQLiteTradingStateStore,
        *,
        testnet: bool = False,
        reconcile_callback: Callable[[], Awaitable[bool]] | None = None,
        lock_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.exchange = exchange
        self.store = store
        self.testnet = bool(testnet)
        self.reconcile_callback = reconcile_callback
        self.lock_callback = lock_callback
        self.connected = False
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._listen_key: str | None = None
        self._seen_events: set[str] = set()
        self._reconcile_task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="binance-user-data-stream")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def _set_connected(self, connected: bool, reason: str | None = None) -> None:
        self.connected = connected
        self.store.set_runtime_state(
            "user_data_stream",
            {"connected": connected, "reason": reason},
        )
        if not connected and self.lock_callback:
            self.lock_callback("USER_STREAM_DISCONNECTED")

    async def _call_exchange_method(self, names: tuple[str, ...], params: dict[str, Any] | None = None) -> Any:
        for name in names:
            method = getattr(self.exchange, name, None)
            if method is not None:
                return await asyncio.to_thread(method, params or {})
        raise RuntimeError(f"exchange does not expose any of: {names}")

    async def _create_listen_key(self) -> str:
        response = await self._call_exchange_method(
            ("fapiPrivatePostListenKey", "fapiPrivatePostListenkey"),
        )
        key = response.get("listenKey") if isinstance(response, dict) else None
        if not key:
            raise RuntimeError("Binance listen key response was empty")
        return str(key)

    async def _keepalive(self) -> None:
        while not self._stop.is_set() and self._listen_key:
            await asyncio.sleep(30 * 60)
            await self._call_exchange_method(
                ("fapiPrivatePutListenKey", "fapiPrivatePutListenkey"),
                {"listenKey": self._listen_key},
            )

    @staticmethod
    def _event_key(payload: dict[str, Any]) -> str:
        order = _as_dict(payload.get("o"))
        account = _as_dict(payload.get("a"))
        return "|".join(
            str(value or "")
            for value in (
                payload.get("e"),
                payload.get("E"),
                payload.get("T"),
                order.get("i"),
                order.get("c"),
                order.get("X"),
                order.get("z"),
                account.get("m"),
            )
        )

    def _handle_order_event(self, payload: dict[str, Any]) -> bool:
        order = _as_dict(payload.get("o"))
        client_id = str(order.get("c") or "")
        if not client_id:
            return False
        record = self.store.get(client_id)
        if record is None:
            return False
        status = str(order.get("X") or "").upper()
        filled = float(order.get("z") or 0.0)
        average = float(order.get("ap") or 0.0)
        event_time = int(payload.get("E") or 0)
        transaction_time = int(payload.get("T") or order.get("T") or 0)
        scope = f"order:{client_id or order.get('i')}"
        previous = self.store.get_order_event_high_water(scope) or {}
        previous_status = str(previous.get("last_status") or "").upper()
        previous_filled = float(previous.get("last_cumulative_filled_qty") or 0.0)
        if filled + 1e-12 < previous_filled:
            return False
        terminal_regression = (
            previous_status in TERMINAL_EXCHANGE_STATUSES
            and STATUS_RANK.get(status, 0) < STATUS_RANK.get(previous_status, 0)
        )
        if terminal_regression and filled <= previous_filled + 1e-12:
            return False
        filled = max(filled, previous_filled)
        if terminal_regression:
            state = OrderState(record.order_state)
        elif status == "PARTIALLY_FILLED":
            state = OrderState.PARTIALLY_FILLED
        elif status == "FILLED":
            intent = OrderIntent(record.order_intent)
            state = (
                OrderState.CLOSING
                if intent in {
                    OrderIntent.CLOSE,
                    OrderIntent.EMERGENCY_CLOSE,
                    OrderIntent.MANUAL_CLOSE,
                    OrderIntent.GRID_EXIT,
                }
                else OrderState.FILLED_UNVERIFIED_LIQUIDATION
            )
        elif status in {"CANCELED", "EXPIRED"}:
            state = OrderState.CANCELED
        elif status == "REJECTED":
            state = OrderState.FAILED
        else:
            state = OrderState.ACKNOWLEDGED
        self.store.transition(
            client_id,
            state,
            exchange_order_id=str(order.get("i") or record.exchange_order_id or "") or None,
            filled_qty=filled,
            average_fill_price=average,
            last_error=str(order.get("r") or "") or None,
        )
        self.store.set_order_event_high_water(
            scope,
            client_order_id=client_id,
            exchange_order_id=str(order.get("i") or "") or None,
            event_time=event_time,
            transaction_time=transaction_time,
            status=status,
            cumulative_filled_qty=filled,
        )
        return True

    @staticmethod
    def _normalize_symbol(value: Any) -> str:
        return "".join(character for character in str(value or "").upper() if character.isalnum())

    def _handle_account_event(self, payload: dict[str, Any]) -> None:
        account = _as_dict(payload.get("a"))
        balances = [
            {
                "asset": item.get("a"),
                "wallet_balance": item.get("wb"),
                "cross_wallet_balance": item.get("cw"),
            }
            for item in account.get("B", [])
            if isinstance(item, dict)
        ]
        positions = [
            {
                "symbol": item.get("s"),
                "amount": item.get("pa"),
                "entry_price": item.get("ep"),
                "unrealized_pnl": item.get("up"),
                "position_side": item.get("ps"),
            }
            for item in account.get("P", [])
            if isinstance(item, dict)
        ]
        self.store.set_runtime_state(
            "last_account_update",
            {
                "event_time": payload.get("E"),
                "reason": account.get("m"),
                "balances": balances,
                "positions": positions,
            },
        )
        active_symbols = {
            self._normalize_symbol(record.symbol)
            for record in self.store.list_by_states(ACTIVE_ORDER_STATES)
        }
        for position in positions:
            try:
                amount = abs(float(position.get("amount") or 0.0))
            except (TypeError, ValueError):
                self.store.set_runtime_state("entry_lock_reason", "RECONCILIATION_REQUIRED:invalid_account_event")
                continue
            symbol = self._normalize_symbol(position.get("symbol"))
            if amount > 0 and symbol not in active_symbols:
                self.store.set_runtime_state(
                    "entry_lock_reason",
                    f"RECONCILIATION_REQUIRED:untracked_position:{position.get('symbol')}",
                )
                if self.lock_callback:
                    self.lock_callback("RECONCILIATION_REQUIRED")

    def _handle_algo_event(self, payload: dict[str, Any]) -> bool:
        order = _as_dict(payload.get("o"))
        status = str(order.get("X") or "").upper()
        client_id = str(order.get("caid") or "")
        algo_id = str(order.get("aid") or "")
        scope = f"algo:{client_id or algo_id}"
        event_time = int(payload.get("E") or payload.get("T") or 0)
        previous = self.store.get_order_event_high_water(scope) or {}
        previous_status = str(previous.get("last_status") or "").upper()
        if (
            previous_status in TERMINAL_EXCHANGE_STATUSES
            and STATUS_RANK.get(status, 0) < STATUS_RANK.get(previous_status, 0)
        ):
            return False
        self.store.set_runtime_state(
            "last_algo_order_event",
            {
                "event_time": payload.get("E") or payload.get("T"),
                "client_algo_id": client_id,
                "algo_id": algo_id,
                "symbol": order.get("s"),
                "status": status,
                "working_type": order.get("wt"),
                "reduce_only": order.get("R"),
                "reject_reason": order.get("rm"),
            },
        )
        self.store.set_order_event_high_water(
            scope,
            client_order_id=client_id or None,
            exchange_order_id=algo_id or None,
            event_time=event_time,
            transaction_time=int(payload.get("T") or 0),
            status=status,
            cumulative_filled_qty=0.0,
        )
        if status in {"REJECTED", "EXPIRED", "CANCELED", "CANCELLED"}:
            record = self.store.get(client_id) if client_id else None
            if record is None and algo_id:
                record = next(
                    (
                        candidate
                        for candidate in self.store.list_by_states(ACTIVE_ORDER_STATES)
                        if str(candidate.stop_order_id or "") == algo_id
                        or algo_id in {str(value) for value in candidate.take_profit_order_ids}
                    ),
                    None,
                )
            is_stop_loss = bool(
                record
                and (
                    record.order_intent == OrderIntent.PROTECTION_SL.value
                    or str(record.stop_order_id or "") == algo_id
                )
            )
            if record and is_stop_loss and record.order_state == OrderState.PROTECTED.value:
                self.store.transition(
                    record.client_order_id,
                    OrderState.FILLED_UNPROTECTED,
                    last_error=f"protection Algo {status}",
                )
            if self.lock_callback:
                self.lock_callback(f"PROTECTION_ALGO_{status}:{order.get('s') or 'UNKNOWN'}")
        return True

    def handle_event(self, payload: dict[str, Any]) -> bool:
        key = self._event_key(payload)
        if key in self._seen_events:
            return False
        event_time = int(payload.get("E") or payload.get("T") or 0)
        event_type = str(payload.get("e") or "")
        handled = True
        if event_type == "ORDER_TRADE_UPDATE":
            handled = self._handle_order_event(payload)
        elif event_type == "ACCOUNT_UPDATE":
            scope = "account"
            previous = self.store.get_order_event_high_water(scope) or {}
            if event_time < int(previous.get("last_event_time") or 0):
                return False
            self._handle_account_event(payload)
            self.store.set_order_event_high_water(
                scope,
                client_order_id=None,
                exchange_order_id=None,
                event_time=event_time,
                transaction_time=int(payload.get("T") or 0),
                status="ACCOUNT_UPDATE",
                cumulative_filled_qty=0.0,
            )
        elif event_type == "ALGO_UPDATE":
            handled = self._handle_algo_event(payload)
        if not handled:
            return False
        self._seen_events.add(key)
        if len(self._seen_events) > 5000:
            self._seen_events = set(list(self._seen_events)[-2500:])
        self.store.set_runtime_state(
            "last_user_stream_event",
            {"event_type": event_type, "event_time": event_time, "event_key": key},
        )
        if event_type in {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE", "ALGO_UPDATE"}:
            immediate = event_type == "ALGO_UPDATE" and str(
                _as_dict(payload.get("o")).get("X") or ""
            ).upper() in {"REJECTED", "EXPIRED"}
            self.schedule_reconciliation(event_type, immediate=immediate)
        return True

    def schedule_reconciliation(self, reason: str, *, immediate: bool = False) -> None:
        if self.reconcile_callback is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._reconcile_task and not self._reconcile_task.done():
            if not immediate:
                return
            self._reconcile_task.cancel()
        self._reconcile_task = asyncio.create_task(
            self._run_scheduled_reconciliation(reason, 0.0 if immediate else 0.5),
            name="binance-user-stream-reconciliation",
        )

    async def _run_scheduled_reconciliation(self, reason: str, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        if self.lock_callback:
            self.lock_callback(f"RECONCILIATION_REQUIRED:{reason}")
        if self.reconcile_callback and not await self.reconcile_callback():
            self._set_connected(False, f"reconciliation_failed:{reason}")
            raise RuntimeError(f"REST reconciliation failed after {reason}")

    async def _consume_messages(self, websocket: Any) -> None:
        async for message in websocket:
            payload = json.loads(message)
            if not isinstance(payload, dict):
                continue
            if payload.get("e") == "listenKeyExpired":
                if self.lock_callback:
                    self.lock_callback("USER_STREAM_LISTEN_KEY_EXPIRED")
                raise RuntimeError("Binance listen key expired")
            self.handle_event(payload)

    async def run(self) -> None:
        try:
            import websockets
        except ImportError:
            self._set_connected(False, "websockets_dependency_missing")
            logger.critical("websockets is required for Binance user data stream")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._listen_key = await self._create_listen_key()
                base = "wss://stream.binancefuture.com/ws" if self.testnet else "wss://fstream.binance.com/ws"
                url = f"{base}/{self._listen_key}"
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as websocket:
                    if self.reconcile_callback and not await self.reconcile_callback():
                        raise RuntimeError("REST reconciliation failed after user stream connect")
                    self._set_connected(True, "connected_and_reconciled")
                    backoff = 1.0
                    self._keepalive_task = asyncio.create_task(
                        self._keepalive(),
                        name="binance-listen-key-keepalive",
                    )
                    consumer = asyncio.create_task(
                        self._consume_messages(websocket),
                        name="binance-user-stream-consumer",
                    )
                    done, pending = await asyncio.wait(
                        {self._keepalive_task, consumer},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    for task in pending:
                        task.cancel()
                    for task in pending:
                        with suppress(asyncio.CancelledError):
                            await task
                    for task in done:
                        await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Binance user data stream disconnected")
                self._set_connected(False, f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
            finally:
                if self._keepalive_task:
                    self._keepalive_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._keepalive_task
                    self._keepalive_task = None

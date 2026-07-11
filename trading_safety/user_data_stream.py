"""Binance USD-M user-data stream with REST reconciliation on reconnect."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
from typing import Any, Awaitable, Callable

from .order_state import ACTIVE_ORDER_STATES, OrderState, SQLiteTradingStateStore


logger = logging.getLogger(__name__)


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

    def _handle_order_event(self, payload: dict[str, Any]) -> None:
        order = _as_dict(payload.get("o"))
        client_id = str(order.get("c") or "")
        if not client_id:
            return
        record = self.store.get(client_id)
        if record is None:
            return
        status = str(order.get("X") or "").upper()
        filled = float(order.get("z") or 0.0)
        average = float(order.get("ap") or 0.0)
        reduce_only = bool(order.get("R"))
        if status == "PARTIALLY_FILLED":
            state = OrderState.PARTIALLY_FILLED
        elif status == "FILLED":
            state = (
                OrderState.CLOSED
                if reduce_only
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

    def _handle_algo_event(self, payload: dict[str, Any]) -> None:
        order = _as_dict(payload.get("o"))
        status = str(order.get("X") or "").upper()
        self.store.set_runtime_state(
            "last_algo_order_event",
            {
                "event_time": payload.get("E") or payload.get("T"),
                "client_algo_id": order.get("caid"),
                "algo_id": order.get("aid"),
                "symbol": order.get("s"),
                "status": status,
                "working_type": order.get("wt"),
                "reduce_only": order.get("R"),
                "reject_reason": order.get("rm"),
            },
        )
        if status in {"REJECTED", "EXPIRED"} and self.lock_callback:
            self.lock_callback(f"PROTECTION_ALGO_{status}:{order.get('s') or 'UNKNOWN'}")

    def handle_event(self, payload: dict[str, Any]) -> bool:
        key = self._event_key(payload)
        if key in self._seen_events:
            return False
        event_time = int(payload.get("E") or payload.get("T") or 0)
        last_event = self.store.get_runtime_state("last_user_stream_event", {}) or {}
        last_event_time = int(last_event.get("event_time") or 0)
        if event_time and last_event_time and event_time < last_event_time:
            logger.warning(
                "Out-of-order user stream event ignored: current=%s last=%s",
                event_time,
                last_event_time,
            )
            return False
        self._seen_events.add(key)
        if len(self._seen_events) > 5000:
            self._seen_events = set(list(self._seen_events)[-2500:])
        event_type = str(payload.get("e") or "")
        if event_type == "ORDER_TRADE_UPDATE":
            self._handle_order_event(payload)
        elif event_type == "ACCOUNT_UPDATE":
            self._handle_account_event(payload)
        elif event_type == "ALGO_UPDATE":
            self._handle_algo_event(payload)
        self.store.set_runtime_state(
            "last_user_stream_event",
            {"event_type": event_type, "event_time": event_time, "event_key": key},
        )
        return True

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
                self._keepalive_task = asyncio.create_task(self._keepalive())
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as websocket:
                    if self.reconcile_callback and not await self.reconcile_callback():
                        raise RuntimeError("REST reconciliation failed after user stream connect")
                    self._set_connected(True, "connected_and_reconciled")
                    backoff = 1.0
                    async for message in websocket:
                        payload = json.loads(message)
                        if isinstance(payload, dict):
                            handled = self.handle_event(payload)
                            if handled and payload.get("e") == "ACCOUNT_UPDATE" and self.reconcile_callback:
                                if self.lock_callback:
                                    self.lock_callback("RECONCILIATION_REQUIRED:ACCOUNT_UPDATE")
                                if not await self.reconcile_callback():
                                    raise RuntimeError("REST reconciliation failed after account update")
                            if payload.get("e") == "listenKeyExpired":
                                raise RuntimeError("Binance listen key expired")
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

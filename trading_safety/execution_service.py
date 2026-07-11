"""Single execution entry point shared by all Binance Futures engines."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .binance_algo_gateway import BinanceAlgoOrderGateway
from .order_gateway import IdempotentOrderGateway, OrderSubmissionResult
from .order_state import OrderIntent, SQLiteTradingStateStore
from .reconciliation import ReconciliationResult, reconcile_exchange_state


class CryptoExecutionService:
    def __init__(
        self,
        exchange: Any,
        state_store: SQLiteTradingStateStore,
        order_gateway: IdempotentOrderGateway,
        algo_gateway: BinanceAlgoOrderGateway | None = None,
        reconciliation_service: Callable[..., Awaitable[ReconciliationResult]] | None = None,
        protection_manager: Any = None,
        liquidation_guard: Any = None,
        accounting_service: Any = None,
    ) -> None:
        self.exchange = exchange
        self.state_store = state_store
        self.order_gateway = order_gateway
        self.algo_gateway = algo_gateway or BinanceAlgoOrderGateway(exchange)
        self.reconciliation_service = reconciliation_service
        self.protection_manager = protection_manager
        self.liquidation_guard = liquidation_guard
        self.accounting_service = accounting_service

    async def submit_entry(self, **kwargs: Any) -> OrderSubmissionResult:
        return await self.order_gateway.submit_entry(**kwargs)

    async def submit_position_add(self, **kwargs: Any) -> OrderSubmissionResult:
        return await self.order_gateway.submit_position_add(**kwargs)

    async def submit_limit_entry(self, **kwargs: Any) -> OrderSubmissionResult:
        return await self.order_gateway.submit_limit_entry_or_add(**kwargs)

    async def submit_reduce_only_close(self, **kwargs: Any) -> OrderSubmissionResult:
        return await self.order_gateway.submit_reduce_only_close(**kwargs)

    async def place_stop_loss(self, **kwargs: Any) -> Any:
        if self.protection_manager is None:
            raise RuntimeError("CRYPTO_PROTECTION_MANAGER_UNAVAILABLE")
        return await self.protection_manager("sl", **kwargs)

    async def place_take_profit(self, **kwargs: Any) -> Any:
        if self.protection_manager is None:
            raise RuntimeError("CRYPTO_PROTECTION_MANAGER_UNAVAILABLE")
        return await self.protection_manager("tp", **kwargs)

    async def cancel_protection(self, **kwargs: Any) -> Any:
        if self.protection_manager is None:
            raise RuntimeError("CRYPTO_PROTECTION_MANAGER_UNAVAILABLE")
        return await self.protection_manager("cancel", **kwargs)

    async def reconcile(self, **kwargs: Any) -> ReconciliationResult:
        if self.reconciliation_service is not None:
            return await self.reconciliation_service(**kwargs)
        return await reconcile_exchange_state(
            self.exchange,
            self.state_store,
            **kwargs,
        )

    @staticmethod
    def close_intent(purpose: str) -> OrderIntent:
        text = str(purpose or "").lower()
        if "emergency" in text or "liquidation" in text or "protection" in text:
            return OrderIntent.EMERGENCY_CLOSE
        if "manual" in text:
            return OrderIntent.MANUAL_CLOSE
        if "grid" in text:
            return OrderIntent.GRID_EXIT
        return OrderIntent.CLOSE

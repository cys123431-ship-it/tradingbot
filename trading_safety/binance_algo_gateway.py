"""Binance USD-M Algo Service adapter for conditional TP/SL orders."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any


CONDITIONAL_TYPES = {
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
}


class AlgoLookupStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class ProtectionOrderLookupUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AlgoLookupResult:
    status: AlgoLookupStatus
    order: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class AlgoOrdersSnapshot:
    ok: bool
    orders: tuple[dict[str, Any], ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ProtectionOrderSnapshot:
    regular_orders_ok: bool
    algo_orders_ok: bool
    orders: tuple[dict[str, Any], ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.regular_orders_ok and self.algo_orders_ok


def _bool_text(value: Any) -> str:
    return "true" if bool(value) else "false"


def normalize_futures_market_id(value: Any) -> str:
    """Return Binance's compact market id for CCXT or exchange symbols."""

    text = str(value or "").upper().strip()
    if ":" in text:
        text = text.split(":", 1)[0]
    return "".join(character for character in text if character.isalnum())


class BinanceAlgoOrderGateway:
    def __init__(self, exchange: Any) -> None:
        self.exchange = exchange

    def _market_id(self, symbol: str) -> str:
        market = None
        if hasattr(self.exchange, "market"):
            try:
                market = self.exchange.market(symbol)
            except Exception:
                market = None
        if isinstance(market, dict) and market.get("id"):
            return str(market["id"])
        return normalize_futures_market_id(symbol)

    @staticmethod
    def normalize(raw: dict[str, Any]) -> dict[str, Any]:
        info = dict(raw or {})
        order_type = info.get("orderType") or info.get("type") or ""
        trigger = info.get("triggerPrice") or info.get("stopPrice")
        return {
            "id": str(info.get("algoId") or "") or None,
            "algoId": info.get("algoId"),
            "clientOrderId": info.get("clientAlgoId"),
            "clientAlgoId": info.get("clientAlgoId"),
            "symbol": info.get("symbol"),
            "type": str(order_type).lower(),
            "orderType": order_type,
            "side": str(info.get("side") or "").lower(),
            "amount": info.get("quantity") or info.get("origQty"),
            "quantity": info.get("quantity") or info.get("origQty"),
            "stopPrice": trigger,
            "triggerPrice": trigger,
            "reduceOnly": str(info.get("reduceOnly", "false")).lower() == "true"
            if not isinstance(info.get("reduceOnly"), bool)
            else info.get("reduceOnly"),
            "closePosition": str(info.get("closePosition", "false")).lower() == "true"
            if not isinstance(info.get("closePosition"), bool)
            else info.get("closePosition"),
            "positionSide": info.get("positionSide"),
            "workingType": info.get("workingType"),
            "priceProtect": info.get("priceProtect"),
            "status": info.get("algoStatus") or info.get("status"),
            "timestamp": info.get("updateTime") or info.get("createTime"),
            "info": info,
            "_protection_source": "binance_algo",
        }

    async def create_conditional_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        quantity: Any,
        *,
        trigger_price: Any,
        client_algo_id: str,
        reduce_only: bool = True,
        working_type: str = "MARK_PRICE",
        price_protect: bool = False,
        position_side: str | None = None,
        close_position: bool = False,
        price: Any = None,
    ) -> dict[str, Any]:
        method = getattr(self.exchange, "fapiPrivatePostAlgoOrder", None)
        if not callable(method):
            raise RuntimeError("Binance Algo Order endpoint is unavailable in the installed exchange adapter")
        order_type_value = str(order_type or "").upper()
        if order_type_value not in CONDITIONAL_TYPES:
            raise ValueError(f"unsupported Binance conditional order type: {order_type}")
        if close_position and order_type_value not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError("closePosition=true is only valid for STOP_MARKET or TAKE_PROFIT_MARKET")
        if close_position and price is not None:
            raise ValueError("closePosition=true market triggers must not include price")
        if not close_position and quantity is None:
            raise ValueError("quantity is required when closePosition is false")

        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": self._market_id(symbol),
            "side": str(side).upper(),
            "type": order_type_value,
            "triggerPrice": trigger_price,
            "clientAlgoId": str(client_algo_id)[:36],
            "workingType": str(working_type).upper(),
            "priceProtect": _bool_text(price_protect),
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            params["quantity"] = quantity
            params["reduceOnly"] = _bool_text(reduce_only)
        if position_side:
            params["positionSide"] = str(position_side).upper()
        if price is not None:
            params["price"] = price
        raw = await asyncio.to_thread(method, params)
        if not isinstance(raw, dict):
            raise RuntimeError("Binance Algo Order returned an invalid response")
        return self.normalize(raw)

    async def fetch_by_client_id(self, client_algo_id: str) -> AlgoLookupResult:
        query = getattr(self.exchange, "fapiPrivateGetAlgoOrder", None)
        if not callable(query):
            return AlgoLookupResult(
                AlgoLookupStatus.UNKNOWN,
                error="Binance Algo lookup endpoint unavailable",
            )
        try:
            raw = await asyncio.to_thread(query, {"clientAlgoId": str(client_algo_id)[:36]})
        except Exception as exc:
            text = str(exc).lower()
            if any(token in text for token in ("unknown order", "does not exist", "-2013", "-4139")):
                return AlgoLookupResult(AlgoLookupStatus.NOT_FOUND)
            return AlgoLookupResult(
                AlgoLookupStatus.UNKNOWN,
                error=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(raw, dict):
            return AlgoLookupResult(
                AlgoLookupStatus.UNKNOWN,
                error="Binance Algo lookup returned invalid response",
            )
        if raw.get("algoId") is None:
            code = str(raw.get("code") or "")
            message = str(raw.get("msg") or "").lower()
            if code in {"-2013", "-4139"} or "does not exist" in message:
                return AlgoLookupResult(AlgoLookupStatus.NOT_FOUND)
            return AlgoLookupResult(
                AlgoLookupStatus.UNKNOWN,
                error="Binance Algo lookup response had no algoId",
            )
        return AlgoLookupResult(AlgoLookupStatus.FOUND, order=self.normalize(raw))

    async def fetch_open_orders(self) -> AlgoOrdersSnapshot:
        query = getattr(self.exchange, "fapiPrivateGetOpenAlgoOrders", None)
        if not callable(query):
            return AlgoOrdersSnapshot(False, error="Binance open Algo endpoint unavailable")
        try:
            raw = await asyncio.to_thread(query, {})
        except Exception as exc:
            return AlgoOrdersSnapshot(False, error=f"{type(exc).__name__}: {exc}")
        if isinstance(raw, dict):
            rows = None
            for key in ("orders", "algoOrders", "rows", "data"):
                if key in raw:
                    rows = raw[key]
                    break
            if rows is None:
                return AlgoOrdersSnapshot(False, error="Binance open Algo response was incomplete")
        elif isinstance(raw, list):
            rows = raw
        else:
            return AlgoOrdersSnapshot(False, error="Binance open Algo response was invalid")
        if not isinstance(rows, list):
            return AlgoOrdersSnapshot(False, error="Binance open Algo rows were invalid")
        return AlgoOrdersSnapshot(
            True,
            tuple(self.normalize(row) for row in rows if isinstance(row, dict)),
        )

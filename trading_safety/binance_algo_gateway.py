"""Binance USD-M Algo Service adapter for conditional TP/SL orders."""

from __future__ import annotations

import asyncio
from typing import Any


CONDITIONAL_TYPES = {
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
}


def _bool_text(value: Any) -> str:
    return "true" if bool(value) else "false"


class BinanceAlgoOrderGateway:
    def __init__(self, exchange: Any) -> None:
        self.exchange = exchange

    def _market_id(self, symbol: str) -> str:
        market = None
        if hasattr(self.exchange, "market"):
            market = self.exchange.market(symbol)
        if isinstance(market, dict) and market.get("id"):
            return str(market["id"])
        return str(symbol).upper().replace(":USDT", "").replace("/", "")

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
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": self._market_id(symbol),
            "side": str(side).upper(),
            "type": order_type_value,
            "quantity": quantity,
            "triggerPrice": trigger_price,
            "clientAlgoId": str(client_algo_id)[:36],
            "workingType": str(working_type).upper(),
            "priceProtect": _bool_text(price_protect),
            "reduceOnly": _bool_text(reduce_only),
            "closePosition": _bool_text(close_position),
        }
        if position_side:
            params["positionSide"] = str(position_side).upper()
        if price is not None:
            params["price"] = price
        raw = await asyncio.to_thread(method, params)
        if not isinstance(raw, dict):
            raise RuntimeError("Binance Algo Order returned an invalid response")
        return self.normalize(raw)

    async def fetch_by_client_id(self, client_algo_id: str) -> dict[str, Any] | None:
        query = getattr(self.exchange, "fapiPrivateGetAlgoOrder", None)
        if not callable(query):
            return None
        try:
            raw = await asyncio.to_thread(query, {"clientAlgoId": str(client_algo_id)[:36]})
        except Exception:
            return None
        return self.normalize(raw) if isinstance(raw, dict) and raw.get("algoId") is not None else None

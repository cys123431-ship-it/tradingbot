"""Small HMAC client for Binance European Options REST endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BinanceOptionsApiError(RuntimeError):
    def __init__(
        self,
        message,
        *,
        code=None,
        status=None,
        uncertain=False,
        rate_limited=False,
        retry_after=0.0,
    ):
        super().__init__(str(message))
        self.code = code
        self.status = status
        self.uncertain = bool(uncertain)
        self.rate_limited = bool(rate_limited)
        self.retry_after = max(0.0, float(retry_after or 0.0))


class BinanceOptionsClient:
    BASE_URL = "https://eapi.binance.com"

    def __init__(
        self,
        api_key="",
        secret_key="",
        *,
        timeout=10,
        base_url=None,
        request_limit_per_minute=300,
    ):
        self.api_key = str(api_key or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self.timeout = max(3, int(timeout or 10))
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self._server_offset_ms = 0
        # Binance currently advertises 400 request-weight/minute for EAPI.
        # Keep a 25% reserve for status/position protection calls and for any
        # process sharing the same public IP.  This limiter is process-wide for
        # this client and protects concurrent asyncio.to_thread() calls too.
        self._request_limit_per_minute = max(
            60, min(300, int(request_limit_per_minute or 300))
        )
        self._request_times = deque()
        self._rate_lock = threading.Lock()
        self._cooldown_until = 0.0

    @property
    def authenticated(self):
        return bool(self.api_key and self.secret_key)

    @staticmethod
    def _decode(payload):
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BinanceOptionsApiError("OPTIONS_API_INVALID_JSON") from exc

    @staticmethod
    def _retry_after_seconds(headers):
        value = headers.get("Retry-After") if headers else None
        if not value:
            return 0.0
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(str(value)).timestamp()
                return max(0.0, target - time.time())
            except (TypeError, ValueError, OverflowError):
                return 0.0

    def _acquire_request_slot(self):
        while True:
            with self._rate_lock:
                now = time.monotonic()
                while self._request_times and now - self._request_times[0] >= 60.0:
                    self._request_times.popleft()
                wait_for = max(0.0, self._cooldown_until - now)
                if len(self._request_times) >= self._request_limit_per_minute:
                    wait_for = max(
                        wait_for,
                        60.0 - (now - self._request_times[0]) + 0.01,
                    )
                if wait_for <= 0:
                    self._request_times.append(now)
                    return
            time.sleep(min(wait_for, 1.0))

    def _apply_rate_backoff(self, seconds):
        duration = max(1.0, float(seconds or 60.0))
        with self._rate_lock:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + duration,
            )

    def _request(self, method, path, params=None, *, signed=False):
        method = str(method or "GET").upper()
        self._acquire_request_slot()
        query = dict(params or {})
        if signed:
            if not self.authenticated:
                raise BinanceOptionsApiError("OPTIONS_API_CREDENTIALS_MISSING")
            query.setdefault("recvWindow", 5000)
            query["timestamp"] = int(time.time() * 1000) + int(self._server_offset_ms)
            unsigned = urlencode(query, doseq=True)
            query["signature"] = hmac.new(
                self.secret_key.encode("utf-8"),
                unsigned.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        encoded = urlencode(query, doseq=True)
        url = f"{self.base_url}{path}"
        if encoded:
            url = f"{url}?{encoded}"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        request = Request(url, method=method, headers=headers, data=None)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return self._decode(response.read())
        except HTTPError as exc:
            payload = b""
            try:
                payload = exc.read()
            except Exception:
                pass
            parsed = {}
            try:
                parsed = self._decode(payload)
            except BinanceOptionsApiError:
                parsed = {}
            message = parsed.get("msg") if isinstance(parsed, dict) else None
            code = parsed.get("code") if isinstance(parsed, dict) else None
            error_text = str(message or "").lower()
            rate_limited = (
                exc.code in {418, 429}
                or code == -1003
                or "too many request" in error_text
            )
            retry_after = self._retry_after_seconds(getattr(exc, "headers", None))
            if rate_limited:
                retry_after = retry_after or 60.0
                self._apply_rate_backoff(retry_after)
            # A 5XX/timeout response to a write does not prove that Binance
            # rejected it.  Callers must query by the stable clientOrderId and
            # must never blindly submit a replacement.
            uncertain = method in {"POST", "PUT", "DELETE"} and (
                int(exc.code or 0) >= 500 or int(exc.code or 0) in {408, 409, 425}
            )
            raise BinanceOptionsApiError(
                message or f"OPTIONS_API_HTTP_{exc.code}",
                code=code,
                status=exc.code,
                uncertain=uncertain,
                rate_limited=rate_limited,
                retry_after=retry_after,
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            uncertain = method in {"POST", "PUT", "DELETE"}
            raise BinanceOptionsApiError(
                f"OPTIONS_API_NETWORK_ERROR: {exc}", uncertain=uncertain
            ) from exc

    def sync_time(self):
        payload = self._request("GET", "/eapi/v1/time")
        server_time = int((payload or {}).get("serverTime") or 0)
        if server_time > 0:
            self._server_offset_ms = server_time - int(time.time() * 1000)
        return payload

    def ping(self):
        return self._request("GET", "/eapi/v1/ping")

    def exchange_info(self):
        return self._request("GET", "/eapi/v1/exchangeInfo")

    def index_price(self, underlying):
        return self._request("GET", "/eapi/v1/index", {"underlying": underlying})

    def mark_price(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/eapi/v1/mark", params)

    def ticker(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/eapi/v1/ticker", params)

    def depth(self, symbol, limit=20):
        return self._request(
            "GET", "/eapi/v1/depth", {"symbol": symbol, "limit": int(limit)}
        )

    def recent_trades(self, symbol, limit=100):
        return self._request(
            "GET",
            "/eapi/v1/trades",
            {"symbol": symbol, "limit": min(100, max(1, int(limit)))},
        )

    def margin_account(self):
        return self._request("GET", "/eapi/v1/marginAccount", signed=True)

    def positions(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/eapi/v1/position", params, signed=True)

    def open_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/eapi/v1/openOrders", params, signed=True)

    def query_order(self, symbol, *, order_id=None, client_order_id=None):
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        elif client_order_id:
            params["clientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        return self._request("GET", "/eapi/v1/order", params, signed=True)

    def new_order(
        self,
        symbol,
        side,
        order_type,
        quantity,
        *,
        price=None,
        time_in_force=None,
        post_only=False,
        reduce_only=False,
        client_order_id=None,
    ):
        params = {
            "symbol": symbol,
            "side": str(side).upper(),
            "type": str(order_type).upper(),
            "quantity": str(quantity),
            "reduceOnly": "true" if reduce_only else "false",
            "newOrderRespType": "RESULT",
            "selfTradePreventionMode": "EXPIRE_TAKER",
        }
        if price is not None:
            params["price"] = str(price)
        if time_in_force:
            params["timeInForce"] = str(time_in_force).upper()
        if post_only:
            params["postOnly"] = "true"
        if client_order_id:
            params["clientOrderId"] = str(client_order_id)
        return self._request("POST", "/eapi/v1/order", params, signed=True)

    def cancel_order(self, symbol, *, order_id=None, client_order_id=None):
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        elif client_order_id:
            params["clientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        return self._request("DELETE", "/eapi/v1/order", params, signed=True)

    def user_trades(self, symbol, *, start_time=None, limit=100):
        params = {"symbol": symbol, "limit": min(1000, max(1, int(limit)))}
        if start_time is not None:
            params["startTime"] = int(start_time)
        return self._request("GET", "/eapi/v1/userTrades", params, signed=True)

    def exercise_records(self, symbol=None, *, start_time=None, limit=100):
        params = {"limit": min(1000, max(1, int(limit)))}
        if symbol:
            params["symbol"] = symbol
        if start_time is not None:
            params["startTime"] = int(start_time)
        return self._request("GET", "/eapi/v1/exerciseRecord", params, signed=True)


__all__ = ("BinanceOptionsApiError", "BinanceOptionsClient")

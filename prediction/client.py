"""Small Predict.fun REST client.

Only read-only helpers are intended for the first integration phase. Mainnet
requires an API key; missing auth is rejected before any network request.
"""

from __future__ import annotations

import json
from urllib import error, parse, request


PREDICT_MAINNET_BASE_URL = "https://api.predict.fun"
PREDICT_TESTNET_BASE_URL = "https://api-testnet.predict.fun"


class PredictAuthRequired(RuntimeError):
    """Raised when a mainnet Predict.fun request lacks the required API key."""

    code = "PREDICTION_MAINNET_AUTH_REQUIRED"


class PredictClient:
    def __init__(self, base_url=PREDICT_TESTNET_BASE_URL, api_key=None, jwt_token=None, timeout=15):
        self.base_url = str(base_url or PREDICT_TESTNET_BASE_URL).rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.jwt_token = str(jwt_token or "").strip()
        self.timeout = timeout

    @classmethod
    def testnet(cls, timeout=15):
        return cls(PREDICT_TESTNET_BASE_URL, timeout=timeout)

    @classmethod
    def mainnet(cls, api_key=None, jwt_token=None, timeout=15):
        return cls(PREDICT_MAINNET_BASE_URL, api_key=api_key, jwt_token=jwt_token, timeout=timeout)

    @property
    def is_mainnet(self):
        return self.base_url.rstrip("/") == PREDICT_MAINNET_BASE_URL

    def _headers(self):
        if self.is_mainnet and not self.api_key:
            raise PredictAuthRequired(self.code_message())
        headers = {
            "Accept": "application/json",
            "User-Agent": "tradingbot-prediction-micro-auto/1.0",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        if self.jwt_token:
            headers["Authorization"] = f"Bearer {self.jwt_token}"
        return headers

    @staticmethod
    def code_message():
        return "PREDICTION_MAINNET_AUTH_REQUIRED"

    def get(self, path, params=None):
        query = parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        req = request.Request(url, headers=self._headers(), method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"message": body}
            raise RuntimeError(f"PREDICT_HTTP_{exc.code}: {payload.get('message') or payload}") from exc

    def post(self, path, payload):
        url = f"{self.base_url}{path}"
        headers = self._headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        req = request.Request(
            url,
            headers=headers,
            data=json.dumps(payload or {}).encode("utf-8"),
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(body)
            except Exception:
                error_payload = {"message": body}
            raise RuntimeError(f"PREDICT_HTTP_{exc.code}: {error_payload.get('message') or error_payload}") from exc

    def get_markets(self, first=20, status=None, category_slug=None):
        return self.get(
            "/v1/markets",
            {
                "first": max(1, int(first or 20)),
                "status": status,
                "categorySlug": category_slug,
            },
        )

    def get_orderbook(self, market_id):
        return self.get(f"/v1/markets/{market_id}/orderbook")

    def get_order(self, order_hash_or_id):
        return self.get(f"/v1/orders/{order_hash_or_id}")

    def create_order(self, order_payload):
        if self.is_mainnet and not self.jwt_token:
            raise PredictAuthRequired("PREDICTION_MAINNET_JWT_REQUIRED")
        return self.post("/v1/orders", order_payload)

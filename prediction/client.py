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
    def __init__(self, base_url=PREDICT_TESTNET_BASE_URL, api_key=None, timeout=15):
        self.base_url = str(base_url or PREDICT_TESTNET_BASE_URL).rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout = timeout

    @classmethod
    def testnet(cls, timeout=15):
        return cls(PREDICT_TESTNET_BASE_URL, timeout=timeout)

    @classmethod
    def mainnet(cls, api_key=None, timeout=15):
        return cls(PREDICT_MAINNET_BASE_URL, api_key=api_key, timeout=timeout)

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

import io
from urllib.error import HTTPError

import pytest

from options_trading import client as client_module
from options_trading.client import BinanceOptionsApiError, BinanceOptionsClient


def _http_error(status, payload, headers=None):
    return HTTPError(
        "https://eapi.binance.com/eapi/v1/order",
        status,
        "error",
        headers or {},
        io.BytesIO(payload.encode("utf-8")),
    )


def test_write_5xx_is_uncertain_and_must_be_reconciled(monkeypatch):
    def fail(*args, **kwargs):
        raise _http_error(503, '{"code":-1000,"msg":"Internal error"}')

    monkeypatch.setattr(client_module, "urlopen", fail)
    client = BinanceOptionsClient("key", "secret")

    with pytest.raises(BinanceOptionsApiError) as caught:
        client._request("POST", "/eapi/v1/order")

    assert caught.value.status == 503
    assert caught.value.uncertain is True
    assert caught.value.rate_limited is False


def test_binance_400_too_many_requests_sets_rate_backoff(monkeypatch):
    def fail(*args, **kwargs):
        raise _http_error(
            400,
            '{"code":-1003,"msg":"Too many requests; current limit is 400 requests per minute"}',
            {"Retry-After": "7"},
        )

    monkeypatch.setattr(client_module, "urlopen", fail)
    client = BinanceOptionsClient("key", "secret")

    with pytest.raises(BinanceOptionsApiError) as caught:
        client._request("GET", "/eapi/v1/mark")

    assert caught.value.rate_limited is True
    assert caught.value.retry_after == pytest.approx(7.0)
    assert caught.value.uncertain is False
    assert client._cooldown_until > 0


def test_option_klines_uses_public_eapi_route_and_bounded_limit(monkeypatch):
    captured = {}
    client = BinanceOptionsClient()

    def request(method, path, params=None, *, signed=False):
        captured.update(
            method=method,
            path=path,
            params=params,
            signed=signed,
        )
        return []

    monkeypatch.setattr(client, "_request", request)

    client.klines("BTC-TEST-C", interval="1m", limit=5000)

    assert captured == {
        "method": "GET",
        "path": "/eapi/v1/klines",
        "params": {"symbol": "BTC-TEST-C", "interval": "1m", "limit": 1000},
        "signed": False,
    }

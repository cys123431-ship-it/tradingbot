import pytest
from unittest.mock import AsyncMock
from kstockbot.app.kis_client import KisClient, KisApiError


def test_validate_kis_response_missing_rt_cd_raises():
    client = KisClient()
    with pytest.raises(KisApiError) as exc:
        client._validate_kis_response("/test", {"output": {}}, require_rt_cd_0=True)
    assert exc.value.rt_cd == "MISSING"


def test_validate_kis_response_nonzero_rt_cd_raises():
    client = KisClient()
    with pytest.raises(KisApiError) as exc:
        client._validate_kis_response(
            "/test",
            {"rt_cd": "1", "msg_cd": "ERR", "msg1": "failed"},
            require_rt_cd_0=True
        )
    assert exc.value.rt_cd == "1"
    assert "failed" in str(exc.value)


def test_validate_kis_response_no_rt_cd_allowed_when_not_required():
    client = KisClient()
    data = {"output": {"x": 1}}
    assert client._validate_kis_response("/test", data, require_rt_cd_0=False) == data


def test_validate_kis_response_zero_rt_cd_success():
    client = KisClient()
    data = {"rt_cd": "0", "output": {"x": 1}}
    assert client._validate_kis_response("/test", data, require_rt_cd_0=True) == data


@pytest.mark.asyncio
async def test_get_price_requires_rt_cd_zero():
    client = KisClient()
    client._get_base_headers = AsyncMock(return_value={})
    client._request = AsyncMock(return_value={"rt_cd": "0", "output": {}})

    await client.get_price("005930")

    kwargs = client._request.call_args.kwargs
    assert kwargs["require_rt_cd_0"] is True


@pytest.mark.asyncio
async def test_get_balance_requires_rt_cd_zero():
    client = KisClient()
    client._get_base_headers = AsyncMock(return_value={})
    client._request = AsyncMock(return_value={"rt_cd": "0", "output1": [], "output2": []})

    await client.get_balance()

    kwargs = client._request.call_args.kwargs
    assert kwargs["require_rt_cd_0"] is True


@pytest.mark.asyncio
async def test_get_buyable_cash_requires_rt_cd_zero():
    client = KisClient()
    client._get_base_headers = AsyncMock(return_value={})
    client._request = AsyncMock(return_value={"rt_cd": "0", "output": {}})

    await client.get_buyable_cash("005930", 80000)

    kwargs = client._request.call_args.kwargs
    assert kwargs["require_rt_cd_0"] is True


@pytest.mark.asyncio
async def test_get_open_orders_requires_rt_cd_zero():
    client = KisClient()
    client._get_base_headers = AsyncMock(return_value={})
    client._request = AsyncMock(return_value={"rt_cd": "0", "output": []})

    await client.get_open_orders()

    kwargs = client._request.call_args.kwargs
    assert kwargs["require_rt_cd_0"] is True

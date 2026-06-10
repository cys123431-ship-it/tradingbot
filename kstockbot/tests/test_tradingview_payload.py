import pytest
from pydantic import ValidationError
from kstockbot.app.tradingview_webhook import TradingViewPayload

def test_valid_payload():
    data = {
        "secret": "mysecret",
        "source": "tradingview",
        "strategy": "swing_v1",
        "action": "BUY",
        "symbol": "005930",
        "price": 80000,
        "time": "2023-10-25T10:00:00Z",
        "timeframe": "1D"
    }
    payload = TradingViewPayload(**data)
    assert payload.symbol == "005930"
    assert payload.action == "BUY"
    assert payload.price == 80000.0

def test_invalid_symbol_not_6_digits():
    data = {
        "secret": "mysecret", "source": "tv", "strategy": "s",
        "action": "BUY", "symbol": "00593", "price": 80000,
        "time": "t", "timeframe": "1D"
    }
    with pytest.raises(ValidationError):
        TradingViewPayload(**data)

def test_invalid_price():
    data = {
        "secret": "mysecret", "source": "tv", "strategy": "s",
        "action": "BUY", "symbol": "005930", "price": -10,
        "time": "t", "timeframe": "1D"
    }
    with pytest.raises(ValidationError):
        TradingViewPayload(**data)

def test_invalid_action():
    data = {
        "secret": "mysecret", "source": "tv", "strategy": "s",
        "action": "JUMP", "symbol": "005930", "price": 100,
        "time": "t", "timeframe": "1D"
    }
    with pytest.raises(ValidationError):
        TradingViewPayload(**data)

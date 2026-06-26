import pytest
from unittest.mock import patch
from kstockbot.app.risk_manager import RiskManager
from kstockbot.app.settings import Settings

@pytest.fixture
def risk_mgr():
    mgr = RiskManager()
    mgr.config["allow_market_order"] = False
    mgr.config["max_order_attempts_per_day"] = 10
    mgr.emergency_stop_active = False
    mgr.state["daily_orders"] = 0
    mgr.state["daily_order_attempts"] = 0
    mgr.state["daily_buys"] = 0
    mgr.state["daily_buy_amount"] = 0
    return mgr

def test_emergency_stop(risk_mgr):
    risk_mgr.emergency_stop("test")
    can_trade, msg = risk_mgr.can_trade_now("BUY")
    assert can_trade is False
    assert "EMERGENCY" in msg
    assert risk_mgr.emergency_stop_active is True

def test_analysis_mode_blocks_trade(risk_mgr):
    old_mode = Settings.MODE
    Settings.MODE = "analysis"
    try:
        can_trade, msg = risk_mgr.can_trade_now("BUY")
        assert can_trade is False
        assert "analysis" in msg
    finally:
        Settings.MODE = old_mode

def test_market_order_rejected(risk_mgr):
    order = {"side": "BUY", "symbol": "005930", "qty": 10, "price": 0, "order_type": "market"}
    is_valid, msg = risk_mgr.validate_order(order, {})
    assert is_valid is False
    assert "Market orders are forbidden" in msg

def test_daily_order_attempts_limit(risk_mgr):
    old_mode = Settings.MODE
    Settings.MODE = "paper"
    try:
        with patch("kstockbot.app.risk_manager.can_buy_now", return_value=(True, "OK")):
            risk_mgr.state["daily_order_attempts"] = 10
            is_valid, msg = risk_mgr.can_place_buy("005930", 10000)
            assert is_valid is False
            assert "Max daily order attempts reached" in msg
    finally:
        Settings.MODE = old_mode

def test_daily_order_limit(risk_mgr):
    old_mode = Settings.MODE
    Settings.MODE = "paper"
    try:
        with patch("kstockbot.app.risk_manager.can_buy_now", return_value=(True, "OK")):
            risk_mgr.state["daily_orders"] = 3
            is_valid, msg = risk_mgr.can_place_buy("005930", 10000)
            assert is_valid is False
            assert "Max daily orders reached" in msg
    finally:
        Settings.MODE = old_mode

def test_sell_qty_zero_rejected(risk_mgr):
    old_mode = Settings.MODE
    Settings.MODE = "paper"
    try:
        with patch("kstockbot.app.risk_manager.can_buy_now", return_value=(True, "OK")):
            is_valid, msg = risk_mgr.can_place_sell("005930", 0)
            assert is_valid is False
            assert "quantity" in msg
    finally:
        Settings.MODE = old_mode

def test_record_attempt_vs_success(risk_mgr):
    order = {"side": "BUY", "symbol": "005930", "qty": 1, "price": 10000}
    
    risk_mgr.record_order_attempt(order)
    assert risk_mgr.state["daily_order_attempts"] == 1
    assert risk_mgr.state["daily_orders"] == 0
    
    risk_mgr.record_order_success(order)
    assert risk_mgr.state["daily_orders"] == 1
    assert risk_mgr.state["daily_buys"] == 1
    assert risk_mgr.state["daily_buy_amount"] == 10000

def test_emergency_stop_records_reason_and_time(risk_mgr):
    risk_mgr.emergency_stop("unit test stop")
    assert risk_mgr.state.get("emergency_stop_active") is True
    assert risk_mgr.state.get("emergency_stop_reason") == "unit test stop"
    assert risk_mgr.state.get("emergency_stop_time")

    risk_mgr.resume_trading()
    assert risk_mgr.state.get("emergency_stop_active") is False
    assert risk_mgr.state.get("resume_trading_time")

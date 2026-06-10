import pytest
import datetime
from unittest.mock import patch, AsyncMock
from kstockbot.app.order_manager import order_manager
from kstockbot.app.market_calendar import now_kst

def test_build_limit_order_invalid_symbol():
    with pytest.raises(ValueError, match="Symbol must be a 6-digit string"):
        order_manager.build_limit_order("BUY", "12345", 80000, 100000)

def test_build_limit_order_qty_zero():
    # 1000 won amount / 80000 price = 0 qty
    with pytest.raises(ValueError, match="Calculated quantity is 0"):
        order_manager.build_limit_order("BUY", "005930", 80000, 1000)

@pytest.mark.asyncio
async def test_approval_expired():
    # Setup pending approval that is 11 minutes old
    old_time = now_kst() - datetime.timedelta(minutes=11)
    order_manager._pending_approvals["test1234"] = {
        "order_id": "test1234-uuid",
        "side": "BUY",
        "symbol": "005930",
        "qty": 1,
        "price": 80000,
        "request_time": old_time.isoformat()
    }
    
    with patch("kstockbot.app.order_manager.OrderManager._save_to_ledger") as mock_ledger:
        res = await order_manager.submit_live_order_after_approval("test1234")
        assert "error" in res
        assert "expired" in res["error"]
        # Check that it was saved to ledger with expired status
        mock_ledger.assert_called_once()
        args, _ = mock_ledger.call_args
        saved_order = args[0]
        assert saved_order["status"] == "approval_expired"

def test_reject_order_records_ledger():
    order_manager._pending_approvals["rej1234"] = {
        "order_id": "rej1234",
        "side": "BUY"
    }
    with patch("kstockbot.app.order_manager.OrderManager._save_to_ledger") as mock_ledger:
        ok, msg = order_manager.reject_order("rej1234")
        assert ok is True
        assert "rejected" in msg
        mock_ledger.assert_called_once()
        args, _ = mock_ledger.call_args
        saved_order = args[0]
        assert saved_order["status"] == "manual_rejected"
        assert saved_order["manual_rejected_time"]

def test_reject_unknown_order_returns_false():
    ok, msg = order_manager.reject_order("missing-id")
    assert ok is False
    assert "not found" in msg

@pytest.mark.asyncio
async def test_live_auto_rejected_in_process_order_request():
    from kstockbot.app.settings import Settings
    old_mode = Settings.MODE
    Settings.MODE = "live"
    try:
        ok, msg = await order_manager.process_order_request("BUY", "005930", 80000, 100000)
        assert ok is False
        assert "disabled in MVP" in msg
    finally:
        Settings.MODE = old_mode

@pytest.mark.asyncio
async def test_approval_submit_attempts_exceeded():
    order_manager._pending_approvals["trymax"] = {
        "order_id": "trymax-uuid",
        "side": "BUY",
        "symbol": "005930",
        "qty": 1,
        "price": 80000,
        "request_time": now_kst().isoformat(),
        "submit_attempts": 3
    }

    with patch("kstockbot.app.order_manager.OrderManager._save_to_ledger") as mock_ledger, \
         patch("kstockbot.app.order_manager.risk_manager.validate_order", return_value=(True, "OK")):
        res = await order_manager.submit_live_order_after_approval("trymax")
        assert "error" in res
        assert "Maximum approval submit attempts exceeded" in res["error"]
        mock_ledger.assert_called_once()


def test_normalize_limit_price_rejects_invalid():
    with pytest.raises(ValueError):
        order_manager.normalize_limit_price(0)
    with pytest.raises(ValueError):
        order_manager.normalize_limit_price(-1)
    with pytest.raises(ValueError):
        order_manager.normalize_limit_price("abc")


def test_normalize_limit_price_rounds_float():
    assert order_manager.normalize_limit_price(80000.4) == 80000
    assert order_manager.normalize_limit_price(80000.6) == 80001


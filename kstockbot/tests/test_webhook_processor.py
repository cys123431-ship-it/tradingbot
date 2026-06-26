import pytest
from unittest.mock import patch, AsyncMock
from kstockbot.app.tradingview_webhook import webhook_processor, TradingViewPayload
from kstockbot.app.settings import Settings

@pytest.fixture
def payload():
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
    return TradingViewPayload(**data)

@pytest.fixture(autouse=True)
def clear_webhook_cache():
    webhook_processor.signal_cache = {}
    yield
    webhook_processor.signal_cache = {}

@pytest.mark.asyncio
async def test_empty_secret_rejected(payload):
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = ""
    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "error"
        assert "not configured" in res["message"]
    finally:
        Settings.WEBHOOK_SECRET = old_secret

@pytest.mark.asyncio
async def test_wrong_secret_rejected(payload):
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "correct_secret"
    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "error"
        assert "Unauthorized" in res["message"]
    finally:
        Settings.WEBHOOK_SECRET = old_secret

@pytest.mark.asyncio
@patch("kstockbot.app.tradingview_webhook.WebhookProcessor._send_telegram_message", new_callable=AsyncMock)
async def test_analysis_mode(mock_send, payload):
    old_mode = Settings.MODE
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.MODE = "analysis"
    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "ok"
        assert "analysis" in res["message"]
    finally:
        Settings.MODE = old_mode
        Settings.WEBHOOK_SECRET = old_secret

@pytest.mark.asyncio
@patch("kstockbot.app.tradingview_webhook.WebhookProcessor._send_telegram_message", new_callable=AsyncMock)
async def test_close_ignored(mock_send, payload):
    old_mode = Settings.MODE
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.MODE = "paper"
    payload.action = "CLOSE"
    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "ignored"
        assert "not implemented" in res["message"]
    finally:
        Settings.MODE = old_mode
        Settings.WEBHOOK_SECRET = old_secret

@pytest.mark.asyncio
@patch("kstockbot.app.tradingview_webhook.WebhookProcessor._send_telegram_message", new_callable=AsyncMock)
async def test_hold_ignored(mock_send, payload):
    old_mode = Settings.MODE
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.MODE = "paper"
    payload.action = "HOLD"
    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "ok"
        assert "Hold signal" in res["message"]
    finally:
        Settings.MODE = old_mode
        Settings.WEBHOOK_SECRET = old_secret

@pytest.mark.asyncio
@patch("kstockbot.app.tradingview_webhook.WebhookProcessor._send_telegram_message", new_callable=AsyncMock)
async def test_live_mode_rejected(mock_send, payload):
    old_mode = Settings.MODE
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.MODE = "live"
    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "error"
        assert "disabled in MVP" in res["message"]
    finally:
        Settings.MODE = old_mode
        Settings.WEBHOOK_SECRET = old_secret

@pytest.mark.asyncio
@patch("kstockbot.app.tradingview_webhook.WebhookProcessor._send_telegram_message", new_callable=AsyncMock)
@patch("kstockbot.app.tradingview_webhook.risk_manager.validate_signal")
@patch("kstockbot.app.tradingview_webhook.order_manager.process_order_request", new_callable=AsyncMock)
async def test_order_manager_failure_returns_error(mock_order, mock_validate_signal, mock_send, payload):
    old_mode = Settings.MODE
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.MODE = "paper"
    mock_validate_signal.return_value = (True, "Signal OK")
    mock_order.return_value = (False, "Paper order failed: test error")
    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "error"
        assert "Paper order failed" in res["message"]
        mock_validate_signal.assert_called_once()
        mock_order.assert_called_once()
    finally:
        Settings.MODE = old_mode
        Settings.WEBHOOK_SECRET = old_secret

@pytest.mark.asyncio
@patch("kstockbot.app.tradingview_webhook.WebhookProcessor._send_telegram_message", new_callable=AsyncMock)
@patch("kstockbot.app.tradingview_webhook.risk_manager.validate_signal")
@patch("kstockbot.app.tradingview_webhook.order_manager.process_order_request", new_callable=AsyncMock)
async def test_risk_rejection_skips_order_manager(mock_order, mock_validate_signal, mock_send, payload):
    old_mode = Settings.MODE
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.MODE = "paper"
    mock_validate_signal.return_value = (False, "After buy window: 15:10")
    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "error"
        assert "Risk rejection" in res["message"]
        assert "After buy window" in res["message"]
        mock_order.assert_not_called()
    finally:
        Settings.MODE = old_mode
        Settings.WEBHOOK_SECRET = old_secret


@pytest.mark.asyncio
@patch("kstockbot.app.tradingview_webhook.WebhookProcessor._send_telegram_message", new_callable=AsyncMock)
@patch("kstockbot.app.tradingview_webhook.risk_manager.validate_signal")
@patch("kstockbot.app.tradingview_webhook.order_manager.process_order_request", new_callable=AsyncMock)
async def test_webhook_passes_float_price_to_order_manager(mock_order, mock_validate_signal, mock_send, payload):
    old_mode = Settings.MODE
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.MODE = "paper"

    payload.price = 80000.75
    payload.time = "2026-06-10T10:01:00+09:00"

    mock_validate_signal.return_value = (True, "Signal OK")
    mock_order.return_value = (True, "Paper order submitted")

    try:
        res = await webhook_processor.process(payload)
        assert res["status"] == "ok"
        args = mock_order.call_args.args
        assert args[2] == 80000.75
    finally:
        Settings.MODE = old_mode
        Settings.WEBHOOK_SECRET = old_secret


def test_validate_secret_only_accepts_valid_secret(payload):
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    try:
        ok, msg = webhook_processor.validate_secret_only(payload)
        assert ok is True
        assert msg == "OK"
    finally:
        Settings.WEBHOOK_SECRET = old_secret


def test_validate_secret_only_rejects_invalid_secret(payload):
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "correct"
    try:
        ok, msg = webhook_processor.validate_secret_only(payload)
        assert ok is False
        assert "Unauthorized" in msg
    finally:
        Settings.WEBHOOK_SECRET = old_secret


@pytest.mark.asyncio
@patch("kstockbot.app.tradingview_webhook.WebhookProcessor._send_telegram_message", new_callable=AsyncMock)
@patch("kstockbot.app.tradingview_webhook.risk_manager.validate_signal")
@patch("kstockbot.app.tradingview_webhook.order_manager.process_order_request", new_callable=AsyncMock)
async def test_duplicate_order_signal_skips_telegram_before_processing(mock_order, mock_validate_signal, mock_send, payload):
    old_mode = Settings.MODE
    old_secret = Settings.WEBHOOK_SECRET
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.MODE = "paper"
    webhook_processor.signal_cache = {}

    mock_validate_signal.return_value = (True, "Signal OK")
    mock_order.return_value = (True, "Paper order submitted")

    try:
        first = await webhook_processor.process(payload)
        second = await webhook_processor.process(payload)

        assert first["status"] == "ok"
        assert second["status"] == "ignored"
        assert "Duplicate" in second["message"]

        # Telegram should be sent only for the first signal, not the duplicate.
        assert mock_send.call_count == 1
        assert mock_order.call_count == 1
    finally:
        Settings.MODE = old_mode
        Settings.WEBHOOK_SECRET = old_secret
        webhook_processor.signal_cache = {}

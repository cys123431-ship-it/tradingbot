from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from kstockbot.app.main import app
from kstockbot.app.settings import Settings


def sample_payload(secret="mysecret"):
    return {
        "secret": secret,
        "source": "tradingview",
        "strategy": "endpoint_test",
        "action": "BUY",
        "symbol": "005930",
        "price": 80000.75,
        "time": "2026-06-10T10:00:00+09:00",
        "timeframe": "1D",
    }


def test_webhook_fast_ack_accepts_valid_secret(monkeypatch):
    old_secret = Settings.WEBHOOK_SECRET
    old_fast_ack = Settings.WEBHOOK_FAST_ACK
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.WEBHOOK_FAST_ACK = True

    async def fake_background(payload, event_id=None):
        return None

    monkeypatch.setattr("kstockbot.app.main.process_webhook_background", fake_background)

    try:
        client = TestClient(app)
        res = client.post("/webhook/tradingview", json=sample_payload())
        assert res.status_code == 202
        data = res.json()
        assert data["status"] == "accepted"
        assert data["fast_ack"] is True
        assert data["symbol"] == "005930"
        assert "event_id" in data
        assert data["event_id"]
    finally:
        Settings.WEBHOOK_SECRET = old_secret
        Settings.WEBHOOK_FAST_ACK = old_fast_ack


def test_webhook_fast_ack_rejects_invalid_secret():
    old_secret = Settings.WEBHOOK_SECRET
    old_fast_ack = Settings.WEBHOOK_FAST_ACK
    Settings.WEBHOOK_SECRET = "correct"
    Settings.WEBHOOK_FAST_ACK = True

    try:
        client = TestClient(app)
        res = client.post("/webhook/tradingview", json=sample_payload(secret="wrong"))
        assert res.status_code == 403
        assert res.json()["status"] == "error"
    finally:
        Settings.WEBHOOK_SECRET = old_secret
        Settings.WEBHOOK_FAST_ACK = old_fast_ack


def test_webhook_sync_mode_preserves_error_status(monkeypatch):
    old_secret = Settings.WEBHOOK_SECRET
    old_fast_ack = Settings.WEBHOOK_FAST_ACK
    Settings.WEBHOOK_SECRET = "mysecret"
    Settings.WEBHOOK_FAST_ACK = False

    async def fake_process(payload):
        return {"status": "error", "message": "Risk rejection: test"}

    monkeypatch.setattr("kstockbot.app.main.webhook_processor.process", fake_process)

    try:
        client = TestClient(app)
        res = client.post("/webhook/tradingview", json=sample_payload())
        assert res.status_code == 400
        assert res.json()["status"] == "error"
        assert "event_id" in res.json()
    finally:
        Settings.WEBHOOK_SECRET = old_secret
        Settings.WEBHOOK_FAST_ACK = old_fast_ack

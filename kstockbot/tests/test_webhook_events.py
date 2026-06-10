from kstockbot.app.webhook_events import create_event, update_event, list_recent_events, last_event_summary
from kstockbot.app.tradingview_webhook import TradingViewPayload


def sample_payload():
    return TradingViewPayload(
        secret="mysecret",
        source="test",
        strategy="unit",
        action="BUY",
        symbol="005930",
        price=80000.75,
        time="2026-06-10T10:00:00+09:00",
        timeframe="1D",
    )


def test_create_and_update_webhook_event():
    payload = sample_payload()
    event_id = create_event(payload, mode="paper", fast_ack=True)

    assert event_id
    summary = last_event_summary()
    assert summary["event_id"] == event_id
    assert summary["status"] == "accepted"

    update_event(event_id, "ok", "Paper order submitted", {"status": "ok", "message": "Paper order submitted"})
    summary = last_event_summary()
    assert summary["event_id"] == event_id
    assert summary["status"] == "ok"
    assert "Paper order" in summary["message"]


def test_list_recent_events_limit():
    payload = sample_payload()
    for _ in range(3):
        create_event(payload, mode="analysis", fast_ack=True)

    events = list_recent_events(2)
    assert len(events) == 2

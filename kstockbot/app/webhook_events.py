import uuid
from typing import Any
from .storage import webhook_events_storage
from .market_calendar import now_kst
from .logger import get_logger

logger = get_logger("webhook_events")

MAX_WEBHOOK_EVENTS = 200


def _load_events() -> list[dict]:
    events = webhook_events_storage.load()
    if not isinstance(events, list):
        return []
    return events


def _save_events(events: list[dict]) -> None:
    webhook_events_storage.save(events[-MAX_WEBHOOK_EVENTS:])


def create_event(payload: Any, mode: str, fast_ack: bool) -> str:
    event_id = str(uuid.uuid4())[:12]
    event = {
        "event_id": event_id,
        "created_at": now_kst().isoformat(),
        "updated_at": now_kst().isoformat(),
        "status": "accepted",
        "mode": mode,
        "fast_ack": bool(fast_ack),
        "source": getattr(payload, "source", ""),
        "strategy": getattr(payload, "strategy", ""),
        "action": getattr(payload, "action", ""),
        "symbol": getattr(payload, "symbol", ""),
        "timeframe": getattr(payload, "timeframe", ""),
        "payload_time": getattr(payload, "time", ""),
        "message": "accepted",
    }
    events = _load_events()
    events.append(event)
    _save_events(events)
    return event_id


def update_event(event_id: str, status: str, message: str = "", result: dict | None = None) -> None:
    events = _load_events()
    found = False
    for event in events:
        if event.get("event_id") == event_id:
            event["status"] = status
            event["updated_at"] = now_kst().isoformat()
            if message:
                event["message"] = str(message)[:300]
            if result is not None:
                safe_result = {
                    "status": result.get("status"),
                    "action": result.get("action"),
                    "symbol": result.get("symbol"),
                    "message": str(result.get("message", ""))[:300],
                }
                event["result"] = safe_result
            found = True
            break

    if not found:
        logger.warning(f"Webhook event not found for update: {event_id}")
        events.append({
            "event_id": event_id,
            "created_at": now_kst().isoformat(),
            "updated_at": now_kst().isoformat(),
            "status": status,
            "message": str(message)[:300],
        })

    _save_events(events)


def list_recent_events(limit: int = 10) -> list[dict]:
    events = _load_events()
    return list(reversed(events[-limit:]))


def last_event_summary() -> dict:
    events = _load_events()
    if not events:
        return {}
    last = events[-1]
    return {
        "event_id": last.get("event_id"),
        "status": last.get("status"),
        "action": last.get("action"),
        "symbol": last.get("symbol"),
        "updated_at": last.get("updated_at"),
        "message": last.get("message"),
    }

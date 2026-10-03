"""One-shot Saturday override of the KST weekend automatic-entry block.

On a Korea-time Saturday the operator may allow automatic entries for 24
hours from the moment of confirmation, at most once per weekend.  Exits and
protection never depend on this; an unreadable state keeps the block.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .decision_journal import OPERATIONS, journal_event

KST = ZoneInfo("Asia/Seoul")
WEEKEND_OVERRIDE_STATE_KEY = "automatic_weekend_trading_override"
WEEKEND_OVERRIDE_HOURS = 24
_SATURDAY = 5
_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")


def _utc(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _kst_text(value):
    parsed = _parse(value)
    return parsed.astimezone(KST).strftime("%m-%d %H:%M") if parsed else "-"


def _load(store):
    if store is None:
        return None
    try:
        payload = store.get_runtime_state(WEEKEND_OVERRIDE_STATE_KEY)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def active_weekend_override(store, now=None):
    """Return the override payload while it is still valid, else None."""
    payload = _load(store)
    if payload is None:
        return None
    activated = _parse(payload.get("activated_at"))
    expires = _parse(payload.get("expires_at"))
    current = _utc(now)
    if activated is None or expires is None or not (activated <= current < expires):
        return None
    return payload


def weekend_override_availability(store, now=None):
    """Return (allowed, reason) for pressing the button now."""
    local = _utc(now).astimezone(KST)
    if local.weekday() != _SATURDAY:
        return False, (
            "한국시간 토요일에만 사용할 수 있습니다 "
            f"(현재 {local:%m-%d}({_WEEKDAY_KO[local.weekday()]}) {local:%H:%M})"
        )
    if store is None:
        return False, "거래 상태 저장소가 준비되지 않았습니다"
    payload = _load(store)
    if payload and payload.get("kst_saturday") == local.date().isoformat():
        return False, (
            "이번 주말에 이미 사용했습니다 "
            f"({_kst_text(payload.get('activated_at'))} ~ {_kst_text(payload.get('expires_at'))} KST)"
        )
    return True, "사용 가능"


def activate_weekend_override(store, now=None):
    """Allow automatic entries for 24h from now; raises ValueError if not allowed."""
    allowed, reason = weekend_override_availability(store, now)
    if not allowed:
        raise ValueError(reason)
    current = _utc(now)
    payload = {
        "kst_saturday": current.astimezone(KST).date().isoformat(),
        "activated_at": current.isoformat(),
        "expires_at": (current + timedelta(hours=WEEKEND_OVERRIDE_HOURS)).isoformat(),
    }
    store.set_runtime_state(WEEKEND_OVERRIDE_STATE_KEY, payload)
    journal_event(
        OPERATIONS, "weekend_override_activated", **payload,
        code_ref="weekend_override.py:activate_weekend_override",
    )
    return payload


def weekend_override_status_text(store, now=None):
    active = active_weekend_override(store, now)
    if active:
        return (
            "🟢 주말 자동진입 허용 중: "
            f"{_kst_text(active.get('activated_at'))} ~ {_kst_text(active.get('expires_at'))} KST"
        )
    return "⚪ 주말 자동진입 허용: 꺼짐 (토·일 신규 자동진입 차단)"


__all__ = (
    "WEEKEND_OVERRIDE_HOURS",
    "WEEKEND_OVERRIDE_STATE_KEY",
    "activate_weekend_override",
    "active_weekend_override",
    "weekend_override_availability",
    "weekend_override_status_text",
)

"""Macro and event-risk guard for UT Breakout.

The first implementation is intentionally config/manual-event driven. It gives
live and research code a shared decision primitive without web scraping.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite


@dataclass(frozen=True)
class MacroRiskEvent:
    start_time: datetime
    end_time: datetime
    severity: str
    event_type: str = "MACRO"
    description: str = ""


@dataclass(frozen=True)
class MacroGuardDecision:
    blocked: bool = False
    reason: str = "MACRO_OK"
    risk_multiplier: float = 1.0
    event: MacroRiskEvent | None = None


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _parse_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def coerce_macro_event(value):
    if isinstance(value, MacroRiskEvent):
        return value
    if not isinstance(value, dict):
        return None
    start = _parse_time(value.get("start_time") or value.get("start") or value.get("time"))
    end = _parse_time(value.get("end_time") or value.get("end") or value.get("time"))
    if start is None:
        return None
    if end is None or end < start:
        end = start
    return MacroRiskEvent(
        start_time=start,
        end_time=end,
        severity=str(value.get("severity") or "HIGH").upper(),
        event_type=str(value.get("event_type") or value.get("type") or "MACRO"),
        description=str(value.get("description") or value.get("name") or ""),
    )


def is_macro_risk_window(now=None, events=None, config=None):
    config = config or {}
    if now is None:
        now = datetime.now(timezone.utc)
    now = _parse_time(now)
    if now is None:
        return False, None
    buffer_min = _finite_float(config.get("macro_event_buffer_minutes"), 30.0)
    blocking = {str(item).upper() for item in config.get("macro_block_severities", ("HIGH", "CRITICAL"))}
    for raw_event in events or ():
        event = coerce_macro_event(raw_event)
        if event is None:
            continue
        start = event.start_time - timedelta(minutes=buffer_min)
        end = event.end_time + timedelta(minutes=buffer_min)
        if start <= now <= end and event.severity.upper() in blocking:
            return True, event
    return False, None


def apply_macro_guard(now=None, events=None, config=None):
    config = config or {}
    manual = bool(config.get("macro_risk_flag", False) or config.get("manual_macro_risk_flag", False))
    blocked, event = is_macro_risk_window(now, events, config)
    if manual and not blocked:
        blocked = True
    if not blocked:
        return MacroGuardDecision()

    mode = str(config.get("macro_guard_mode", "BLOCK") or "BLOCK").upper()
    if mode == "REDUCE":
        return MacroGuardDecision(
            False,
            "MACRO_RISK_REDUCED",
            _finite_float(config.get("macro_risk_multiplier"), 0.5),
            event,
        )
    return MacroGuardDecision(True, "MACRO_RISK_BLOCK", 0.0, event)

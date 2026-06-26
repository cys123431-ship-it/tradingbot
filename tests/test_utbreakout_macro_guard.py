from datetime import datetime, timedelta, timezone

from utbreakout.macro_guard import MacroRiskEvent, apply_macro_guard, is_macro_risk_window


def test_macro_guard_blocks_high_severity_event_window():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    event = MacroRiskEvent(now + timedelta(minutes=10), now + timedelta(minutes=20), "HIGH", "CPI", "CPI")

    blocked, matched = is_macro_risk_window(now, [event], {"macro_event_buffer_minutes": 30})

    assert blocked is True
    assert matched == event


def test_macro_guard_allows_outside_event_window():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    event = MacroRiskEvent(now + timedelta(hours=3), now + timedelta(hours=4), "HIGH", "FOMC", "FOMC")

    blocked, matched = is_macro_risk_window(now, [event], {"macro_event_buffer_minutes": 30})

    assert blocked is False
    assert matched is None


def test_macro_guard_can_reduce_risk_instead_of_block():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    event = MacroRiskEvent(now, now + timedelta(minutes=1), "CRITICAL", "EXCHANGE", "outage")

    decision = apply_macro_guard(
        now,
        [event],
        {"macro_guard_mode": "REDUCE", "macro_risk_multiplier": 0.4},
    )

    assert decision.blocked is False
    assert decision.reason == "MACRO_RISK_REDUCED"
    assert decision.risk_multiplier == 0.4

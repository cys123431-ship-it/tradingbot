"""One-shot Saturday (KST) override of the weekend automatic-entry block."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import emas
from bot_runtime import weekend_override
from bot_runtime.controller_automatic_controls import (
    ControllerAutomaticTradingControlsMixin,
)
from bot_runtime.weekend_override import (
    activate_weekend_override,
    active_weekend_override,
    weekend_override_availability,
)
from trading_safety.order_state import SQLiteTradingStateStore

KST = ZoneInfo("Asia/Seoul")
SAT = datetime(2026, 10, 3, 15, 0, tzinfo=KST)   # Saturday
SUN = datetime(2026, 10, 4, 10, 0, tzinfo=KST)   # Sunday
FRI = datetime(2026, 10, 2, 23, 0, tzinfo=KST)   # Friday


@pytest.fixture
def store(tmp_path):
    state = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    yield state
    state.close()


@pytest.mark.parametrize("moment,allowed", [(SAT, True), (SUN, False), (FRI, False)])
def test_button_is_only_available_on_korean_saturday(store, moment, allowed):
    ok, reason = weekend_override_availability(store, moment)
    assert ok is allowed
    if not allowed:
        assert "토요일에만" in reason


def test_override_lasts_24_hours_and_is_once_per_weekend(store):
    payload = activate_weekend_override(store, SAT)
    assert payload["kst_saturday"] == "2026-10-03"

    assert active_weekend_override(store, SAT + timedelta(hours=23, minutes=59))
    assert active_weekend_override(store, SAT + timedelta(hours=24)) is None
    assert active_weekend_override(store, SAT - timedelta(minutes=1)) is None

    with pytest.raises(ValueError, match="이미 사용"):
        activate_weekend_override(store, SAT + timedelta(hours=2))
    # Next Saturday it can be used again.
    assert activate_weekend_override(store, SAT + timedelta(days=7))


def test_sunday_cannot_activate(store):
    with pytest.raises(ValueError, match="토요일에만"):
        activate_weekend_override(store, SUN)
    assert active_weekend_override(store, SUN) is None


def _engine(store):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.trading_state_store = store
    return engine


def test_engine_weekend_block_respects_the_active_override(store):
    engine = _engine(store)
    assert engine._automatic_weekend_entry_block_reason(SAT) is not None

    activate_weekend_override(store, SAT)

    assert engine._automatic_weekend_entry_block_reason(SAT + timedelta(minutes=1)) is None
    # Still inside 24h on Sunday morning.
    assert engine._automatic_weekend_entry_block_reason(SAT + timedelta(hours=20)) is None
    # Expired on Sunday afternoon -> blocked again.
    assert engine._automatic_weekend_entry_block_reason(SAT + timedelta(hours=24, minutes=1)) is not None
    # Weekdays are never blocked regardless of the override.
    assert engine._automatic_weekend_entry_block_reason(FRI) is None


def test_engine_without_store_keeps_the_block():
    engine = _engine(None)
    assert engine._automatic_weekend_entry_block_reason(SAT) is not None


def _controller(store):
    controller = ControllerAutomaticTradingControlsMixin()
    controller.engines = {"signal": SimpleNamespace(trading_state_store=store)}
    return controller


def test_telegram_flow_requires_confirmation_and_works_once(store, monkeypatch):
    real_utc = weekend_override._utc
    monkeypatch.setattr(
        weekend_override, "_utc", lambda now=None: real_utc(now or SAT)
    )
    controller = _controller(store)

    text, markup = controller._weekend_override_prompt()
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "wkd:do" in callbacks
    assert "사용 가능" in text
    assert active_weekend_override(store, SAT) is None  # nothing until confirmed

    first = asyncio.run(controller._run_weekend_override())
    assert first.startswith("✅ 주말 자동진입 허용 (24시간)")
    assert "10-03 15:00 ~ 10-04 15:00" in first

    second = asyncio.run(controller._run_weekend_override())
    assert "이미 사용" in second
    _, markup = controller._weekend_override_prompt()
    assert "wkd:do" not in [b.callback_data for row in markup.inline_keyboard for b in row]


def test_main_keyboard_has_weekend_button():
    controller = emas.MainController.__new__(emas.MainController)
    labels = [b.text for row in controller._build_main_keyboard().keyboard for b in row]
    assert "/weekend" in labels

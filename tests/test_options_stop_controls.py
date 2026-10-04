"""Futures strategy activation turns options off; STOP turns everything off."""
import asyncio
import re
from pathlib import Path

from tests.test_options_btc_only import _controller
from tests.test_utbreakout_helpers import (
    _FakeTelegramUpdate,
    _emas_module,
    _telegram_controller,
)

ROOT = Path(__file__).parents[1] / "bot_runtime"
HOOK = "await self._turn_off_options_for_futures_strategy()"


def _with_notes(controller):
    notes = []

    async def notify_plain(text, event_type=None):
        notes.append(text)

    controller.notify_plain = notify_plain
    return notes


def test_futures_activation_turns_options_off_once():
    controller = _controller({"btc_only": True, "enabled": True})
    notes = _with_notes(controller)
    assert asyncio.run(controller._turn_off_options_for_futures_strategy()) is True
    assert controller.cfg.data["options_trading"]["enabled"] is False
    assert len(notes) == 1 and "옵션 자동 신규진입을 OFF" in notes[0]
    # Already off: nothing to do, no second message.
    assert asyncio.run(controller._turn_off_options_for_futures_strategy()) is False
    assert len(notes) == 1


def test_stop_closes_bot_options_and_disables_prediction():
    controller = _controller({"btc_only": True, "enabled": True})
    controller.cfg.data["prediction_micro_auto"] = {"enabled": True}
    service = controller.options_trading_service
    service.state["active_position"] = {"symbol": "BTC-261009-85000-C"}
    lines = asyncio.run(controller._stop_all_auxiliary_trading())
    assert controller.cfg.data["options_trading"]["enabled"] is False
    assert controller.cfg.data["prediction_micro_auto"]["enabled"] is False
    assert service.cycles == [(False, True)]  # forced exit requested
    assert any("BTC-261009-85000-C 청산 요청" in line for line in lines)
    assert "Prediction Micro Auto: OFF" in lines


def test_stop_without_options_position_just_switches_off():
    controller = _controller({"btc_only": True, "enabled": False})
    lines = asyncio.run(controller._stop_all_auxiliary_trading())
    assert lines == ["옵션: 신규진입 OFF (이미 꺼져 있었음)"]
    assert controller.options_trading_service.cycles == []


def test_stop_button_runs_futures_stop_then_options_stop():
    controller = _telegram_controller(chat_id=12345)
    order = []

    async def emergency_stop():
        order.append("futures")
        return {"status": "no_position", "cancelled_orders": 0}

    async def auxiliary():
        order.append("auxiliary")
        return ["옵션: 신규진입 OFF"]

    controller.emergency_stop = emergency_stop
    controller._stop_all_auxiliary_trading = auxiliary
    update = _FakeTelegramUpdate(12345, "STOP")
    result = asyncio.run(controller.global_handler(update, None))
    assert result == _emas_module().ConversationHandler.END
    assert order == ["futures", "auxiliary"]
    assert len(update.message.replies) == 1
    reply = str(update.message.replies[0])
    assert "긴급 정지 완료" in reply and "옵션: 신규진입 OFF" in reply


def _function_body(source, name):
    match = re.search(rf"\n(\s*)async def {name}\(.*?\n(.*?)(?=\n\1async def |\Z)", source, re.S)
    assert match, name
    return match.group(2)


def test_every_futures_activation_path_turns_options_off():
    source = (ROOT / "controller_telegram_setup.py").read_text(encoding="utf-8")
    for name in (
        "_activate_utbot_strategy",
        "_activate_utbreak_strategy",
        "_activate_relative_strength_pullback_strategy",
        "_activate_dual_alpha_strategy",
        "_activate_volatility_managed_trend_strategy",
        "_activate_triple_alpha_strategy",
        "_activate_liquidation_exhaustion_reversal_strategy",
        "_activate_quad_alpha_strategy",
        "_activate_crowding_unwind_strategy",
        "_activate_adaptive_breakout_trend_strategy",
        "_enable_utbreak_direct_watchlist",
        "_enable_utbreak_auto_bundle",
    ):
        assert HOOK in _function_body(source, name), name
    # Deactivation paths must not touch options.
    for name in ("_stop_utbreak_trading", "_disable_utbreak_auto_bundle"):
        assert HOOK not in _function_body(source, name), name
    assert (ROOT / "controller_ema200_utbot_rsi.py").read_text(encoding="utf-8").count(HOOK) == 2
    assert HOOK in (ROOT / "controller_custom_entry.py").read_text(encoding="utf-8")


def test_ema200_entry_on_turns_options_off_but_entry_off_does_not():
    source = (ROOT / "controller_ema200_utbot_rsi.py").read_text(encoding="utf-8")
    block = source[source.index('if action == "entry_toggle":'):]
    block = block[: block.index("return")]
    assert 'if not cfg["enabled"]:\n                    ' + HOOK in block

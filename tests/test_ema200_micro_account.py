"""EMA200 micro-account mode (< 100 USDT until > 500 USDT futures equity)."""
import asyncio
import inspect
from types import SimpleNamespace

import pytest

import emas
from bot_runtime.ema200_session import (
    current_ema200_account_mode,
    update_ema200_account_mode,
)
from bot_runtime.ema200_utbot_rsi import (
    EMA200_ACCOUNT_MODE_STATE_KEY,
    build_ema200_utbot_rsi_risk_plan,
    ema200_micro_min_notional_bump,
    ema200_profit_stop_start_for_mode,
    normalize_ema200_utbot_rsi_config,
    resolve_ema200_account_mode,
)
from trading_safety.order_state import SQLiteTradingStateStore


@pytest.mark.parametrize("equity,previous,expected", [
    (55.0, None, "micro"),
    (99.9, "standard", "micro"),
    (300.0, "micro", "micro"),        # stays micro until > 500
    (300.0, "standard", "standard"),  # stays standard until < 100
    (300.0, None, "standard"),        # fresh account in the band
    (500.0, "micro", "micro"),
    (500.01, "micro", "standard"),
    (0.0, "micro", "micro"),          # unknown equity keeps the memory
])
def test_account_mode_hysteresis(equity, previous, expected):
    assert resolve_ema200_account_mode(equity, previous, {}) == expected


def test_micro_mode_can_be_disabled():
    assert resolve_ema200_account_mode(55.0, None, {"micro_account_enabled": False}) == "standard"


def test_micro_plan_risks_five_percent_with_a_milder_ladder_and_a_stop():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=55.0, free_balance=55.0, entry_price=100.0, config={},
        consecutive_losses=0, stop_percent=3.0, account_mode="micro",
    )
    assert plan["sizing_mode"] == "micro_account_atr_risk"
    assert plan["account_mode"] == "micro"
    assert plan["leverage"] == 5
    assert plan["emergency_stop_required"] is True
    assert plan["risk_budget_usdt"] == pytest.approx(2.75)
    assert plan["planned_notional"] == pytest.approx(91.6667, rel=1e-4)
    assert plan["planned_emergency_loss_usdt"] == pytest.approx(2.75)

    # After 4 losses the micro cap is 30% margin (82.5 notional), not 10%.
    streak = build_ema200_utbot_rsi_risk_plan(
        account_equity=55.0, free_balance=55.0, entry_price=100.0, config={},
        consecutive_losses=4, stop_percent=3.0, account_mode="micro",
    )
    assert streak["margin_cap_percent"] == 30.0
    assert streak["planned_notional"] == pytest.approx(82.5)


def test_standard_mode_keeps_revision2_sizing():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=300.0, free_balance=300.0, entry_price=100.0, config={},
        consecutive_losses=0, stop_percent=3.0, account_mode="standard",
    )
    assert plan["sizing_mode"] == "small_account_atr_risk"
    assert plan["risk_budget_usdt"] == pytest.approx(4.5)


def test_micro_profit_stop_starts_later():
    cfg = normalize_ema200_utbot_rsi_config({})
    assert ema200_profit_stop_start_for_mode(cfg, "micro") == 25.0
    assert ema200_profit_stop_start_for_mode(cfg, "standard") == 15.0
    assert ema200_profit_stop_start_for_mode(cfg, None) == 15.0


def _micro_plan(stop=3.0, equity=55.0):
    return build_ema200_utbot_rsi_risk_plan(
        account_equity=equity, free_balance=equity, entry_price=100.0, config={},
        consecutive_losses=0, stop_percent=stop, account_mode="micro",
    )


def test_min_notional_bump_is_bounded_by_risk_and_margin():
    plan = _micro_plan(stop=3.0)  # budget 2.75 USDT
    # BTC-like 100 USDT minimum: loss 3.03 <= 1.5 x 2.75 -> bumped.
    assert ema200_micro_min_notional_bump(plan, 100.0, 270.0, 1.5) == pytest.approx(101.0)
    # Wide stop: 100 x 5% = 5.05 > 4.125 -> refused.
    assert ema200_micro_min_notional_bump(_micro_plan(stop=5.0), 100.0, 270.0, 1.5) is None
    # Not enough margin for the minimum -> refused.
    assert ema200_micro_min_notional_bump(plan, 100.0, 90.0, 1.5) is None
    # Never outside micro mode.
    standard = build_ema200_utbot_rsi_risk_plan(
        account_equity=55.0, free_balance=55.0, entry_price=100.0, config={},
        consecutive_losses=0, stop_percent=3.0, account_mode="standard",
    )
    assert ema200_micro_min_notional_bump(standard, 100.0, 270.0, 1.5) is None


def test_account_mode_is_persisted_and_reports_changes_once(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    try:
        assert update_ema200_account_mode(store, 55.0, {}) == ("micro", None, True)
        assert update_ema200_account_mode(store, 320.0, {}) == ("micro", "micro", False)
        assert current_ema200_account_mode(store) == "micro"
        assert update_ema200_account_mode(store, 520.0, {}) == ("standard", "micro", True)
        assert update_ema200_account_mode(store, 320.0, {}) == ("standard", "standard", False)
        state = store.get_runtime_state(EMA200_ACCOUNT_MODE_STATE_KEY)
        assert state["previous_mode"] == "micro" and state["equity_usdt"] == 520.0
    finally:
        store.close()


def test_entry_wires_mode_bump_and_profit_start():
    entry_source = inspect.getsource(emas.SignalEntryMixin.entry)
    assert "update_ema200_account_mode(" in entry_source
    assert "account_mode=ema200_account_mode" in entry_source
    assert "ema200_micro_min_notional_bump(" in entry_source
    scanner_source = inspect.getsource(emas.SignalEngine._ema200_apply_margin_profit_stop)
    assert "ema200_profit_stop_start_for_mode(" in scanner_source


def test_sizing_preview_shows_micro_mode():
    from tests.test_ema200_utbot_rsi_strategy import _registered_telegram_controller

    controller = _registered_telegram_controller()

    async def balance():
        return 55.0, 55.0, 0.0

    controller.engines = {"signal": SimpleNamespace(get_balance_info=balance, trading_state_store=None)}
    text = asyncio.run(
        controller._ema200_utbot_rsi_sizing_preview(controller._ema200_utbot_rsi_config(), 0)
    )
    assert "계좌 모드: 극소액" in text
    assert "1회 손실: 계좌의 5% = 2.75 USDT" in text

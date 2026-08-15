import json

import pytest

from options_trading import OptionsTradingService
from options_trading.config import OPTIONS_CAPITAL_LIMIT_USDT, normalize_options_config
from options_trading.risk import build_long_option_entry_plan


def test_options_sleeve_uses_full_fixed_hundred_usdt_budget():
    cfg = normalize_options_config({"entry_fraction": 0.90, "capital_limit_usdt": 999})
    assert cfg["entry_fraction"] == 1.0
    assert cfg["capital_limit_usdt"] == OPTIONS_CAPITAL_LIMIT_USDT
    assert cfg["capital_limit_usdt"] == 100.0


def test_twenty_one_usdt_account_can_trade_inside_hundred_usdt_cap():
    cfg = normalize_options_config({})
    plan = build_long_option_entry_plan(
        ask_price=19.0,
        index_price=2000.0,
        unit=1,
        min_qty=1,
        step_size=1,
        cash_bankroll_usdt=21.0,
        entry_fraction=cfg["entry_fraction"],
        capital_limit_usdt=cfg["capital_limit_usdt"],
    )

    assert plan["accepted"] is True
    assert plan["quantity"] == "1"
    assert 18.0 < plan["total_entry_cost_usdt"] <= 21.0
    assert plan["spend_cap_usdt"] == 21.0
    assert plan["hard_cap_usdt"] == 21.0


def test_hundred_usdt_sleeve_can_buy_contract_above_old_twenty_usdt_cap():
    plan = build_long_option_entry_plan(
        ask_price=75.0,
        index_price=2000.0,
        unit=1,
        min_qty=1,
        step_size=1,
        cash_bankroll_usdt=100.0,
        entry_fraction=1.0,
        capital_limit_usdt=100.0,
    )

    assert plan["accepted"] is True
    assert plan["quantity"] == "1"
    assert 20.0 < plan["total_entry_cost_usdt"] <= 100.0
    assert plan["hard_cap_usdt"] == 100.0


def test_full_budget_still_rejects_contract_above_hundred_usdt_with_fee():
    plan = build_long_option_entry_plan(
        ask_price=100.0,
        index_price=2000.0,
        unit=1,
        min_qty=1,
        step_size=1,
        cash_bankroll_usdt=100.0,
        entry_fraction=1.0,
        capital_limit_usdt=100.0,
    )

    assert plan["accepted"] is False
    assert plan["reason"] == "OPTION_PREMIUM_PLUS_FEE_EXCEEDS_HARD_CAP"


def test_legacy_twenty_usdt_state_migrates_once_and_preserves_pnl(tmp_path):
    state_path = tmp_path / "options_state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                # Simulate 7.5 USDT of cumulative loss under the old 20 USDT sleeve.
                "cash_bankroll_usdt": 12.5,
                "active_position": None,
                "trades": [],
            }
        ),
        encoding="utf-8",
    )

    kwargs = dict(
        config_getter=lambda: {"enabled": False},
        credentials_getter=lambda: {},
        market_data_exchange=object(),
        state_path=state_path,
        client_factory=lambda **kwargs: None,
    )
    first = OptionsTradingService(**kwargs)
    assert first.state["capital_limit_usdt"] == 100.0
    assert first.state["cash_bankroll_usdt"] == pytest.approx(92.5)

    second = OptionsTradingService(**kwargs)
    assert second.state["capital_limit_usdt"] == 100.0
    assert second.state["cash_bankroll_usdt"] == pytest.approx(92.5)

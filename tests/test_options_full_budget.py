from options_trading.config import OPTIONS_CAPITAL_LIMIT_USDT, normalize_options_config
from options_trading.risk import build_long_option_entry_plan


def test_options_sleeve_uses_full_fixed_twenty_usdt_budget():
    cfg = normalize_options_config({"entry_fraction": 0.90, "capital_limit_usdt": 999})
    assert cfg["entry_fraction"] == 1.0
    assert cfg["capital_limit_usdt"] == OPTIONS_CAPITAL_LIMIT_USDT


def test_twenty_one_usdt_account_can_buy_contract_costing_between_eighteen_and_twenty():
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
    assert 18.0 < plan["total_entry_cost_usdt"] <= 20.0
    assert plan["spend_cap_usdt"] == 20.0
    assert plan["hard_cap_usdt"] == 20.0


def test_full_budget_still_rejects_contract_above_twenty_usdt_with_fee():
    plan = build_long_option_entry_plan(
        ask_price=20.0,
        index_price=2000.0,
        unit=1,
        min_qty=1,
        step_size=1,
        cash_bankroll_usdt=21.0,
        entry_fraction=1.0,
        capital_limit_usdt=20.0,
    )

    assert plan["accepted"] is False
    assert plan["reason"] == "OPTION_PREMIUM_PLUS_FEE_EXCEEDS_HARD_CAP"

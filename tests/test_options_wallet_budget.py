"""Options sizing uses the live options-wallet balance, not a fixed 100 USDT ledger."""
import asyncio

import pytest

from options_trading.config import (
    normalize_options_config,
    options_budget_text,
    options_spend_limit,
)
from options_trading.risk import build_long_option_entry_plan
from options_trading.service import OptionsTradingService
from tests.test_options_trading import _service


@pytest.mark.parametrize(
    "cap,available,expected",
    [(0.0, 33.0, 33.0), (20.0, 33.0, 20.0), (50.0, 33.0, 33.0), (0.0, -5.0, 0.0), (0.0, None, 0.0)],
)
def test_spend_limit_is_wallet_balance_with_optional_cap(cap, available, expected):
    assert options_spend_limit({"capital_limit_usdt": cap}, available) == pytest.approx(expected)


def test_budget_text():
    assert "고정 한도 없음" in options_budget_text({"capital_limit_usdt": 0})
    assert "최대 40 USDT" in options_budget_text({"capital_limit_usdt": 40})


def _plan(bankroll, ask, cap=0.0):
    return build_long_option_entry_plan(
        ask_price=ask, index_price=60000.0, unit=1, min_qty=0.01, step_size=0.01,
        cash_bankroll_usdt=bankroll, entry_fraction=1.0, capital_limit_usdt=cap,
    )


def test_thirty_three_usdt_wallet_can_buy_one_btc_lot():
    plan = _plan(33.0, 2500.0)  # 0.01 BTC option at 2,500 premium = 25 USDT
    assert plan["accepted"] is True
    assert plan["quantity"] == "0.01"
    assert plan["hard_cap_usdt"] == pytest.approx(33.0)
    assert plan["total_entry_cost_usdt"] <= 33.0


def test_no_fixed_hundred_ceiling_any_more():
    plan = _plan(250.0, 15000.0)  # 150 USDT for 0.01
    assert plan["accepted"] is True
    assert plan["total_entry_cost_usdt"] > 100.0
    # An explicit cap still binds.
    assert _plan(250.0, 15000.0, cap=100.0)["accepted"] is False


def test_depleted_legacy_ledger_no_longer_blocks_entries(tmp_path):
    # The old 100 USDT ledger only shrank after losses and never refilled on
    # deposit.  With 21 USDT in the options wallet the bot must still trade.
    service, clients = _service(tmp_path, enabled=True)
    service.state["cash_bankroll_usdt"] = 0.5
    service._save_state()
    result = asyncio.run(service.run_cycle(force_scan=True))
    assert result["action"] == "entered"
    assert clients[0].orders[0]["side"] == "BUY"
    assert service.state["active_position"]["entry_total_usdt"] <= 21.0


def test_status_reports_wallet_budget(tmp_path):
    service, _ = _service(tmp_path, enabled=False)
    status = asyncio.run(service.status_snapshot(refresh=False))
    assert status["capital_limit_usdt"] == 0.0
    assert "고정 한도 없음" in status["budget_text"]
    assert status["realized_pnl_usdt"] == 0.0


def test_cap_text_rewrite_no_longer_corrupts_amounts():
    assert OptionsTradingService._rewrite_cap_text("손익 +1.20 USDT") == "손익 +1.20 USDT"


def test_persisted_config_is_stable_after_migration():
    first = normalize_options_config({"capital_limit_usdt": 100.0})
    assert normalize_options_config(first) == first

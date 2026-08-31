from __future__ import annotations

import asyncio
from types import SimpleNamespace

from trading_safety.protection_coordinator import manage_open_position_protection
from utbreakout.small_account.risk import classify_stop_geometry
from utbreakout.small_account.state import (
    apply_protection_state_contract,
    is_managed_position_state,
)
from utbreakout.small_account.strategy import small_account_profit_lock_enabled


def test_rebuilt_state_separates_management_from_trailing_activation():
    state = {
        "side": "long",
        "entry_price": 0.0567635,
        "last_stop_price": 0.05746,
        "active": False,
    }

    apply_protection_state_contract(
        state,
        existing_state={"side": "long", "active": True},
        stop_price=0.05746,
    )

    assert state["active"] is False
    assert state["managed_position"] is True
    assert state["profit_lock_armed"] is True
    assert state["protection_status"] == "PROTECTED"
    assert is_managed_position_state(state, side="long") is True


def test_managed_winner_stop_is_not_invalidated_by_entry_or_crossed_mark():
    live = classify_stop_geometry(
        side="long",
        stop_price=0.05746,
        entry_price=0.0567635,
        mark_price=0.05800,
        bot_managed=True,
    )
    crossed = classify_stop_geometry(
        side="long",
        stop_price=0.05746,
        entry_price=0.0567635,
        mark_price=0.05740,
        bot_managed=True,
    )
    external = classify_stop_geometry(
        side="long",
        stop_price=0.05746,
        entry_price=0.0567635,
        mark_price=0.05800,
        bot_managed=False,
    )

    assert live.valid and not live.crossed_live_mark
    assert crossed.valid and crossed.crossed_live_mark
    assert external.valid is False


def test_small_account_strategy_owns_profit_lock_intent_only_when_active():
    assert small_account_profit_lock_enabled({
        "small_account_aggressive_active": True,
        "small_account_roe_profit_lock_enabled": True,
    }) is True
    assert small_account_profit_lock_enabled({
        "small_account_aggressive_active": False,
        "small_account_roe_profit_lock_enabled": True,
    }) is False


def test_position_protection_coordinator_serializes_same_symbol():
    async def scenario():
        active = 0
        maximum = 0

        async def trailing(*_args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"state": "ok"}

        async def pyramid(*_args):
            return {"status": "WAITING"}

        async def fetch(_symbol):
            return True, {
                "side": "long",
                "contracts": 1.0,
                "entryPrice": 100.0,
            }

        engine = SimpleNamespace(
            _get_utbreakout_trailing_state=lambda _symbol: {},
            _manage_utbreakout_partial_trailing=trailing,
            _maybe_apply_aggressive_growth_pyramiding=pyramid,
            _manage_live_ladder_exit_policy=lambda *_args: asyncio.sleep(0),
            _fetch_server_position_checked=fetch,
        )
        position = {"side": "long", "contracts": 1.0, "entryPrice": 100.0}
        results = await asyncio.gather(
            manage_open_position_protection(engine, "BTC/USDT", position, None, {}),
            manage_open_position_protection(engine, "BTC/USDT", position, None, {}),
        )
        return maximum, results

    maximum, results = asyncio.run(scenario())

    assert maximum == 1
    assert [result["status"] for result in results] == ["MANAGED", "MANAGED"]

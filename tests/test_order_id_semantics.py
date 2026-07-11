import re

import pytest

from trading_safety.order_state import (
    OrderRecord,
    OrderState,
    SQLiteTradingStateStore,
    build_client_order_id,
)


def test_order_purposes_have_distinct_deterministic_ids():
    purposes = (
        "entry",
        "position_add",
        "close_manual",
        "close_emergency",
        "close_stop_loss",
        "close_daily_loss",
        "close_take_profit",
        "close_trailing_stop",
        "protection_sl",
        "protection_tp1",
        "protection_tp2",
    )
    ids = {
        purpose: build_client_order_id(
            "UTBreakout",
            "BTC/USDT:USDT",
            "LONG",
            123456,
            purpose,
        )
        for purpose in purposes
    }

    assert len(set(ids.values())) == len(purposes)
    assert ids["close_manual"] == build_client_order_id(
        "UTBreakout", "BTC/USDT:USDT", "LONG", 123456, "close_manual"
    )
    assert all(len(value) <= 36 for value in ids.values())
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in ids.values())


def test_leg_and_revision_are_part_of_order_identity():
    base = build_client_order_id("S", "ETH/USDT", "SHORT", 1, "protection_tp", leg="tp1")
    other_leg = build_client_order_id("S", "ETH/USDT", "SHORT", 1, "protection_tp", leg="tp2")
    revised = build_client_order_id(
        "S", "ETH/USDT", "SHORT", 1, "protection_tp", leg="tp1", revision=2
    )

    assert len({base, other_leg, revised}) == 3


@pytest.mark.parametrize(
    "state",
    [OrderState.PLANNED.value, OrderState.ACKNOWLEDGED.value, OrderState.PROTECTED.value],
)
def test_active_entry_states_block_new_entries(tmp_path, state):
    store = SQLiteTradingStateStore(tmp_path / f"{state}.sqlite3")
    store.upsert(
        OrderRecord(
            f"entry-{state}",
            "BTC/USDT",
            "LONG",
            "UTB",
            "1",
            1.0,
            order_state=state,
        )
    )
    assert store.entry_block_reason("ETH/USDT").startswith(state)

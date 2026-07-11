import asyncio

from trading_safety.order_state import OrderRecord, OrderState, SQLiteTradingStateStore
from trading_safety.user_data_stream import BinanceUserDataStream


def _order_event(event_time, status, cumulative, *, client_id="cid-1"):
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": event_time,
        "T": event_time,
        "o": {
            "i": 1,
            "c": client_id,
            "X": status,
            "z": str(cumulative),
            "ap": "100",
        },
    }


def test_different_event_types_do_not_share_global_timestamp(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    store.upsert(OrderRecord("cid-1", "BTCUSDT", "LONG", "UTB", "1", 20))
    stream = BinanceUserDataStream(object(), store)

    assert stream.handle_event({"e": "ACCOUNT_UPDATE", "E": 200, "T": 200, "a": {}})
    assert stream.handle_event(_order_event(100, "PARTIALLY_FILLED", 10))
    assert store.get("cid-1").filled_qty == 10


def test_order_high_water_prevents_regression_but_accepts_larger_fill(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    store.upsert(OrderRecord("cid-1", "BTCUSDT", "LONG", "UTB", "1", 20))
    stream = BinanceUserDataStream(object(), store)

    assert stream.handle_event(_order_event(200, "PARTIALLY_FILLED", 10))
    assert stream.handle_event(_order_event(100, "PARTIALLY_FILLED", 5)) is False
    assert stream.handle_event(_order_event(150, "PARTIALLY_FILLED", 15))
    assert store.get("cid-1").filled_qty == 15
    assert stream.handle_event(_order_event(300, "FILLED", 20))
    assert stream.handle_event(_order_event(400, "NEW", 20)) is False
    assert store.get("cid-1").order_state == OrderState.FILLED_UNVERIFIED_LIQUIDATION.value
    assert stream.handle_event(_order_event(250, "PARTIALLY_FILLED", 25))
    assert store.get("cid-1").filled_qty == 25
    assert store.get("cid-1").order_state == OrderState.FILLED_UNVERIFIED_LIQUIDATION.value


def test_rejected_algo_locks_and_schedules_immediate_reconciliation(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        calls = []
        locks = []

        async def reconcile():
            calls.append("reconcile")
            return True

        stream = BinanceUserDataStream(
            object(), store, reconcile_callback=reconcile, lock_callback=locks.append
        )
        payload = {
            "e": "ALGO_UPDATE",
            "E": 10,
            "T": 10,
            "o": {"caid": "sl-1", "aid": 1, "s": "BTCUSDT", "X": "REJECTED"},
        }
        assert stream.handle_event(payload)
        await asyncio.sleep(0)
        await stream._reconcile_task
        assert calls == ["reconcile"]
        assert locks[0].startswith("PROTECTION_ALGO_REJECTED")
        assert "RECONCILIATION_REQUIRED" in locks[-1]

    asyncio.run(scenario())


def test_rejected_stop_algo_marks_linked_entry_unprotected(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    store.upsert(
        OrderRecord(
            "entry-1",
            "BTCUSDT",
            "LONG",
            "UTB",
            "1",
            1.0,
            order_state=OrderState.PROTECTED.value,
            stop_order_id="7",
        )
    )
    stream = BinanceUserDataStream(object(), store)
    assert stream.handle_event(
        {
            "e": "ALGO_UPDATE",
            "E": 10,
            "T": 10,
            "o": {"caid": "sl-client", "aid": 7, "s": "BTCUSDT", "X": "REJECTED"},
        }
    )
    assert store.get("entry-1").order_state == OrderState.FILLED_UNPROTECTED.value

from pathlib import Path

from trading_safety.order_state import (
    OrderRecord,
    OrderState,
    SQLiteTradingStateStore,
    atomic_write_json,
    build_client_order_id,
)


def test_client_order_id_is_deterministic_and_signal_specific():
    first = build_client_order_id("UTBreakout", "BTC/USDT:USDT", "LONG", 123456, "entry")
    same = build_client_order_id("UTBreakout", "BTC/USDT:USDT", "LONG", 123456, "entry")
    next_signal = build_client_order_id("UTBreakout", "BTC/USDT:USDT", "LONG", 123457, "entry")

    assert first == same
    assert first != next_signal
    assert len(first) <= 36
    assert all(character.isalnum() or character in "-_" for character in first)


def test_order_state_persists_across_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = SQLiteTradingStateStore(path)
    record = OrderRecord("cid-1", "BTC/USDT:USDT", "LONG", "UTB", "1", 0.1)
    first.upsert(record)
    first.transition(
        "cid-1",
        OrderState.FILLED_UNPROTECTED,
        exchange_order_id="123",
        filled_qty=0.1,
        average_fill_price=100.0,
    )
    first.close()

    second = SQLiteTradingStateStore(path)
    restored = second.get("cid-1")

    assert restored.order_state == OrderState.FILLED_UNPROTECTED.value
    assert restored.exchange_order_id == "123"
    assert second.entry_block_reason("BTC/USDT:USDT").startswith("FILLED_UNPROTECTED")


def test_symbol_lookup_matches_ccxt_and_binance_forms(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    store.upsert(
        OrderRecord(
            client_order_id="recon-hmstr",
            symbol="HMSTR/USDT:USDT",
            side="LONG",
            strategy="EXTERNAL_OR_PRE_RECONCILIATION",
            signal_timestamp="1",
            requested_qty=10,
            filled_qty=10,
            order_state=OrderState.FILLED_UNPROTECTED.value,
        )
    )

    assert [record.client_order_id for record in store.active_for_symbol("HMSTR/USDT")] == [
        "recon-hmstr"
    ]
    assert [record.client_order_id for record in store.active_for_symbol("HMSTRUSDT")] == [
        "recon-hmstr"
    ]
    assert store.entry_block_reason("HMSTR/USDT").startswith("FILLED_UNPROTECTED")

    store.transition("recon-hmstr", OrderState.CLOSED)
    assert store.active_for_symbol("HMSTR/USDT") == []


def test_trade_result_is_idempotent(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    trade = {"trade_id": "trade-1", "symbol": "BTC/USDT", "net_pnl_usdt": 1.0}

    assert store.record_trade_result(trade) is True
    assert store.record_trade_result(trade) is False
    assert len(store.load_trade_results()) == 1


def test_atomic_json_write_replaces_complete_document(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"version": 1})
    atomic_write_json(path, {"version": 2, "ok": True})

    assert path.read_text(encoding="utf-8").strip().startswith("{")
    assert '"version": 2' in path.read_text(encoding="utf-8")
    assert not list(Path(tmp_path).glob("*.tmp"))

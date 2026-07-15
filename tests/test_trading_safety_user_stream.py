from trading_safety.order_state import OrderRecord, OrderState, SQLiteTradingStateStore
from trading_safety.user_data_stream import BinanceUserDataStream


def test_user_stream_deduplicates_fill_and_updates_state(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    store.upsert(OrderRecord("cid-1", "BTCUSDT", "LONG", "UTB", "1", 1.0))
    stream = BinanceUserDataStream(object(), store)
    payload = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 100,
        "T": 100,
        "o": {"i": 1, "c": "cid-1", "X": "FILLED", "z": "1", "ap": "100", "R": False},
    }

    assert stream.handle_event(payload) is True
    assert stream.handle_event(payload) is False
    assert store.get("cid-1").order_state == OrderState.FILLED_UNVERIFIED_LIQUIDATION.value


def test_user_stream_ignores_out_of_order_event_and_disconnect_locks(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    locks = []
    stream = BinanceUserDataStream(object(), store, lock_callback=locks.append)
    assert stream.handle_event({"e": "ACCOUNT_UPDATE", "E": 200, "T": 200, "a": {}}) is True
    assert stream.handle_event({"e": "ACCOUNT_UPDATE", "E": 100, "T": 100, "a": {"m": "ORDER"}}) is False

    stream._set_connected(False, "network_lost")

    assert locks == ["USER_STREAM_DISCONNECTED"]


def test_account_update_persists_balance_and_locks_untracked_position(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    locks = []
    stream = BinanceUserDataStream(object(), store, lock_callback=locks.append)
    payload = {
        "e": "ACCOUNT_UPDATE",
        "E": 300,
        "T": 300,
        "a": {
            "m": "ORDER",
            "B": [{"a": "USDT", "wb": "100", "cw": "100"}],
            "P": [{"s": "BTCUSDT", "pa": "0.1", "ep": "100", "up": "1", "ps": "BOTH"}],
        },
    }

    assert stream.handle_event(payload) is True
    snapshot = store.get_runtime_state("last_account_update")
    assert snapshot["balances"][0]["asset"] == "USDT"
    assert store.get_runtime_state("entry_lock_reason").startswith("RECONCILIATION_REQUIRED")
    assert locks == ["RECONCILIATION_REQUIRED"]


def test_algo_rejection_locks_new_entries(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    locks = []
    stream = BinanceUserDataStream(object(), store, lock_callback=locks.append)
    payload = {
        "e": "ALGO_UPDATE",
        "E": 400,
        "T": 400,
        "o": {
            "caid": "utbslbtc",
            "aid": 7,
            "s": "BTCUSDT",
            "X": "REJECTED",
            "wt": "MARK_PRICE",
            "R": True,
            "rm": "Reduce Only reject",
        },
    }

    assert stream.handle_event(payload) is True
    assert locks == ["PROTECTION_ALGO_REJECTED:BTCUSDT"]
    assert store.get_runtime_state("last_algo_order_event")["working_type"] == "MARK_PRICE"


def test_account_update_matches_ccxt_usdc_symbol_to_binance_market_id(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    store.upsert(
        OrderRecord(
            "cid-usdc",
            "LTC/USDC:USDC",
            "LONG",
            "UTB",
            "1",
            1.0,
            order_state=OrderState.PROTECTED.value,
        )
    )
    locks = []
    stream = BinanceUserDataStream(object(), store, lock_callback=locks.append)

    stream.handle_event(
        {
            "e": "ACCOUNT_UPDATE",
            "E": 500,
            "T": 500,
            "a": {
                "m": "ORDER",
                "P": [
                    {"s": "LTCUSDC", "pa": "1", "ep": "80", "up": "0", "ps": "BOTH"}
                ],
            },
        }
    )

    assert locks == []
    assert store.get_runtime_state("entry_lock_reason") is None

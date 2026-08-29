import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from emas import DBManager, SignalRuntimeMixin
from trading_safety.order_state import (
    OrderIntent,
    OrderRecord,
    OrderState,
    SQLiteTradingStateStore,
)
from trading_safety.trade_accounting import (
    record_closed_trade_accounting,
    resolve_closed_trade_accounting,
)


def test_db_manager_lists_all_open_trades(tmp_path):
    db = DBManager(str(tmp_path / "trades.db"))
    try:
        db.log_trade_entry("BLESS/USDT:USDT", "long", 1.0, 2.0, strategy="vmt")
        db.log_trade_entry("BICO/USDT:USDT", "long", 2.0, 3.0, strategy="vmt")
        db.log_trade_close("BICO/USDT:USDT", 1.0, 1.0, 2.02, "closed")

        assert db.get_open_trades() == [
            {
                "symbol": "BLESS/USDT:USDT",
                "side": "long",
                "entry_price": 1.0,
                "quantity": 2.0,
                "entry_time": db.get_latest_open_trade("BLESS/USDT:USDT")["entry_time"],
                "strategy": "vmt",
            }
        ]
    finally:
        db.conn.close()


def test_db_manager_preserves_resolved_exchange_exit_time(tmp_path):
    db = DBManager(str(tmp_path / "trades.db"))
    try:
        db.log_trade_entry("BLESS/USDT:USDT", "long", 1.0, 2.0, strategy="vmt")
        resolved = "2026-08-03T18:58:36+00:00"

        assert db.log_trade_close(
            "BLESS/USDT:USDT",
            -1.0,
            -10.0,
            0.9,
            "reconciled",
            exit_time=resolved,
        ) is True
        row = db.conn.execute(
            "SELECT exit_time FROM trades WHERE symbol=?",
            ("BLESS/USDT:USDT",),
        ).fetchone()
        assert row[0] == resolved
    finally:
        db.conn.close()


def test_manual_pyramided_close_records_total_qty_reason_and_net_r(tmp_path):
    db = DBManager(str(tmp_path / "trades.db"))
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    symbol = "ZEC/USDT:USDT"
    db.log_trade_entry(
        symbol,
        "long",
        800.0,
        0.695,
        strategy="adaptive_breakout_trend_v1",
    )
    entry_time = datetime.fromisoformat(
        db.get_latest_open_trade(symbol)["entry_time"]
    )
    base_ms = int(entry_time.timestamp() * 1000)
    store.upsert(
        OrderRecord(
            client_order_id="zec-entry",
            exchange_order_id="entry-order",
            symbol=symbol,
            side="LONG",
            strategy="adaptive_breakout_trend_v1",
            signal_timestamp="1",
            requested_qty=0.695,
            filled_qty=0.695,
            average_fill_price=800.0,
            order_state=OrderState.PROTECTED.value,
            metadata={
                "entry_plan_summary": {
                    "adaptive_trend_initial_risk_distance": 15.0,
                }
            },
        )
    )
    store.upsert(
        OrderRecord(
            client_order_id="zec-add-1",
            exchange_order_id="add-order",
            symbol=symbol,
            side="LONG",
            strategy="adaptive_breakout_trend_v1",
            signal_timestamp="2",
            requested_qty=0.161,
            filled_qty=0.161,
            average_fill_price=810.0,
            order_intent=OrderIntent.POSITION_ADD.value,
            order_state=OrderState.PROTECTED.value,
        )
    )

    class _Exchange:
        def fetch_my_trades(self, *_args, **_kwargs):
            return [
                {
                    "timestamp": base_ms - 100,
                    "side": "buy",
                    "amount": 0.695,
                    "price": 800.0,
                    "order": "entry-order",
                    "fee": {"cost": 0.278},
                },
                {
                    "timestamp": base_ms + 1_000,
                    "side": "buy",
                    "amount": 0.161,
                    "price": 810.0,
                    "order": "add-order",
                    "fee": {"cost": 0.130},
                },
                {
                    "timestamp": base_ms + 2_000,
                    "side": "sell",
                    "amount": 0.856,
                    "price": 812.0,
                    "order": "manual-exchange-order",
                    "realizedPnl": 10.0,
                    "fee": {"cost": 0.350},
                },
            ]

    engine = SimpleNamespace(
        db=db,
        exchange=_Exchange(),
        trading_state_store=store,
    )
    engine._utbreakout_plan_symbol_keys = lambda value: [value]
    engine._utbreakout_entry_record_for_symbol = (
        lambda *_args, **_kwargs: store.get("zec-entry")
    )

    try:
        outcome = asyncio.run(record_closed_trade_accounting(
            engine,
            symbol,
            "scanner position completed",
            state={
                "_require_exchange_fills": True,
                "adaptive_trend_initial_risk_distance": 15.0,
                # Price proximity must not relabel a different exchange order
                # as the bot's stop-loss fill.
                "last_stop_price": 812.0,
            },
            persist_live_trade=lambda trade, store: store.upsert_trade_result(trade),
        ))

        row = db.conn.execute(
            "SELECT quantity, exit_reason FROM trades WHERE symbol=?",
            (symbol,),
        ).fetchone()
        trade_result = store.load_trade_results()[0]
        assert outcome["status"] == "RECORDED"
        assert outcome["closed_qty"] == pytest.approx(0.856)
        assert row[0] == pytest.approx(0.856)
        assert row[1] == "manual/external exchange close detected"
        assert trade_result["filled_qty"] == pytest.approx(0.856)
        assert trade_result["exit_legs"][0]["label"] == "EXTERNAL_EXIT"
        assert trade_result["risk_budget_usdt"] == pytest.approx(0.695 * 15.0)
        assert trade_result["realized_r"] == pytest.approx(
            (10.0 - 0.278 - 0.130 - 0.350) / (0.695 * 15.0)
        )
    finally:
        store.close()
        db.conn.close()


def test_db_manager_archives_legacy_open_trade_without_inventing_exit(tmp_path):
    db = DBManager(str(tmp_path / "trades.db"))
    try:
        db.log_trade_entry("OLD/USDT:USDT", "long", 1.0, 2.0, strategy="legacy")
        entry_time = db.get_latest_open_trade("OLD/USDT:USDT")["entry_time"]

        assert db.archive_open_trade(
            "OLD/USDT:USDT",
            entry_time,
            "exchange flat; identity unavailable",
        ) is True
        assert db.get_latest_open_trade("OLD/USDT:USDT") is None
        assert db.get_open_trades() == []
        row = db.conn.execute(
            """SELECT exit_time, pnl_usdt, reconciliation_archived_at,
            reconciliation_archive_reason FROM trades WHERE symbol=?""",
            ("OLD/USDT:USDT",),
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2]
        assert "exchange flat" in row[3]
    finally:
        db.conn.close()


def test_reconciliation_accounts_flat_trade_but_skips_live_position():
    class _DB:
        def get_open_trades(self):
            return [
                {
                    "symbol": "BLESS/USDT:USDT",
                    "side": "long",
                    "quantity": 10.0,
                    "entry_time": "2026-08-03T03:35:00.900000+00:00",
                },
                {
                    "symbol": "BICO/USDT:USDT",
                    "side": "long",
                    "quantity": 10.0,
                    "entry_time": "2026-08-03T19:01:00.900000+00:00",
                },
            ]

    class _Store:
        def records_for_symbol(self, symbol):
            if symbol != "BLESS/USDT:USDT":
                return []
            return [
                SimpleNamespace(
                    order_intent="ENTRY",
                    order_state="CLOSED",
                    metadata={"reconciled_without_exchange_position": True},
                    filled_qty=10.0,
                    requested_qty=10.0,
                    side="LONG",
                    created_at="2026-08-03T03:35:00+00:00",
                    strategy="quad_alpha_v1",
                )
            ]

    class _Engine(SignalRuntimeMixin):
        def __init__(self):
            self.db = _DB()
            self.trading_state_store = _Store()
            self.accounted = []

        def _futures_symbol_key(self, symbol):
            return str(symbol).replace("/", "").replace(":USDT", "")

        async def _record_closed_trade_accounting(self, symbol, reason, *, state=None):
            self.accounted.append((symbol, reason, state))
            return {"status": "RECORDED"}

    engine = _Engine()
    result = SimpleNamespace(
        snapshot_complete=True,
        positions_ok=True,
        positions=[{"symbol": "BICO/USDT:USDT", "contracts": 10}],
        closed_position_symbols=["BLESS/USDT:USDT"],
    )

    outcomes = asyncio.run(engine._account_for_reconciled_flat_trades(result))

    assert [item[0] for item in engine.accounted] == ["BLESS/USDT:USDT"]
    assert outcomes == [{"symbol": "BLESS/USDT:USDT", "status": "RECORDED"}]


def test_unrelated_historical_order_for_same_symbol_is_not_backfilled():
    class _DB:
        def get_open_trades(self):
            return [{
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "quantity": 1.0,
                "entry_time": "2026-05-01T13:54:13+00:00",
            }]

    class _Store:
        def records_for_symbol(self, _symbol):
            return [SimpleNamespace(
                order_intent="ENTRY",
                order_state="CLOSED",
                metadata={"reconciled_without_exchange_position": True},
                filled_qty=1.0,
                requested_qty=1.0,
                side="LONG",
                created_at="2026-07-18T14:06:53+00:00",
            )]

    class _Engine(SignalRuntimeMixin):
        db = _DB()
        trading_state_store = _Store()

        def _futures_symbol_key(self, symbol):
            return str(symbol).replace("/", "").replace(":USDT", "")

        async def _record_closed_trade_accounting(self, *_args, **_kwargs):
            raise AssertionError("unrelated trade must not be accounted")

    result = SimpleNamespace(
        snapshot_complete=True,
        positions_ok=True,
        positions=[],
        closed_position_symbols=["BTC/USDT:USDT"],
    )

    assert asyncio.run(_Engine()._account_for_reconciled_flat_trades(result)) == []


def test_exchange_flat_legacy_row_without_durable_identity_is_archived():
    class _DB:
        def __init__(self):
            self.archived = []

        def get_open_trades(self):
            return [{
                "symbol": "OLD/USDT:USDT",
                "side": "long",
                "quantity": 1.0,
                "entry_time": "2026-05-01T13:54:13+00:00",
            }]

        def archive_open_trade(self, symbol, entry_time, reason):
            self.archived.append((symbol, entry_time, reason))
            return True

    class _Store:
        def records_for_symbol(self, _symbol):
            return []

    class _Engine(SignalRuntimeMixin):
        def __init__(self):
            self.db = _DB()
            self.trading_state_store = _Store()

        def _futures_symbol_key(self, symbol):
            return str(symbol).replace("/", "").replace(":USDT", "")

        async def _record_closed_trade_accounting(self, *_args, **_kwargs):
            raise AssertionError("unverified legacy PnL must not be synthesized")

    engine = _Engine()
    result = SimpleNamespace(
        snapshot_complete=True,
        positions_ok=True,
        positions=[],
        closed_position_symbols=["OLD/USDT:USDT"],
    )

    outcomes = asyncio.run(engine._account_for_reconciled_flat_trades(result))

    assert engine.db.archived
    assert outcomes == [{
        "symbol": "OLD/USDT:USDT",
        "status": "ARCHIVED_UNVERIFIED_LEGACY",
        "entry_time": "2026-05-01T13:54:13+00:00",
    }]


def test_terminal_order_identity_does_not_keep_legacy_trade_open_forever():
    class _DB:
        def __init__(self):
            self.archived = []

        def get_open_trades(self):
            return [{
                "symbol": "OLD/USDT:USDT",
                "side": "long",
                "quantity": 1.0,
                "entry_time": "2026-05-01T13:54:13+00:00",
            }]

        def archive_open_trade(self, symbol, entry_time, reason):
            self.archived.append((symbol, entry_time, reason))
            return True

    class _Store:
        def records_for_symbol(self, _symbol):
            return [SimpleNamespace(
                order_intent="ENTRY",
                order_state="CLOSED",
                metadata={"close_reason": "scanner position completed"},
                filled_qty=1.0,
                requested_qty=1.0,
                side="LONG",
                created_at="2026-05-01T13:54:12+00:00",
            )]

    class _Engine(SignalRuntimeMixin):
        def __init__(self):
            self.db = _DB()
            self.trading_state_store = _Store()

        def _futures_symbol_key(self, symbol):
            return str(symbol).replace("/", "").replace(":USDT", "")

        async def _record_closed_trade_accounting(self, *_args, **_kwargs):
            raise AssertionError("unverified legacy PnL must not be synthesized")

    engine = _Engine()
    result = SimpleNamespace(
        snapshot_complete=True,
        positions_ok=True,
        positions=[],
        closed_position_symbols=["OLD/USDT:USDT"],
    )

    outcomes = asyncio.run(engine._account_for_reconciled_flat_trades(result))

    assert engine.db.archived
    assert outcomes[0]["status"] == "ARCHIVED_UNVERIFIED_LEGACY"


def test_recent_scanner_close_is_backfilled_from_durable_order_identity():
    now = datetime.now(timezone.utc).isoformat()

    class _DB:
        def get_open_trades(self):
            return [{
                "symbol": "SNDK/USDT:USDT",
                "side": "long",
                "quantity": 0.13,
                "entry_time": now,
            }]

    class _Store:
        def records_for_symbol(self, _symbol):
            return [SimpleNamespace(
                order_intent="ENTRY",
                order_state="CLOSED",
                metadata={"close_reason": "scanner position completed"},
                filled_qty=0.13,
                requested_qty=0.13,
                side="LONG",
                created_at=now,
                updated_at=now,
                strategy="adaptive_breakout_trend_v1",
            )]

    class _Engine(SignalRuntimeMixin):
        def __init__(self):
            self.db = _DB()
            self.trading_state_store = _Store()
            self.states = []

        def _futures_symbol_key(self, symbol):
            return str(symbol).replace("/", "").replace(":USDT", "")

        async def _record_closed_trade_accounting(self, _symbol, _reason, *, state=None):
            self.states.append(state)
            return {"status": "RECORDED"}

    engine = _Engine()
    result = SimpleNamespace(
        snapshot_complete=True,
        positions_ok=True,
        positions=[],
        closed_position_symbols=[],
    )

    outcomes = asyncio.run(engine._account_for_reconciled_flat_trades(result))

    assert outcomes == [{"symbol": "SNDK/USDT:USDT", "status": "RECORDED"}]
    assert engine.states[0]["_require_exchange_fills"] is True


def test_exchange_fill_required_accounting_rejects_estimated_ticker_fallback():
    class _DB:
        def __init__(self):
            self.closed = False

        def get_latest_open_trade(self, _symbol):
            return {
                "symbol": "SNDK/USDT:USDT",
                "side": "long",
                "entry_price": 100.0,
                "quantity": 1.0,
                "entry_time": "2026-08-14T00:00:00+00:00",
            }

        def log_trade_close(self, *_args, **_kwargs):
            self.closed = True
            return True

    class _Exchange:
        def fetch_my_trades(self, *_args, **_kwargs):
            return []

        def fetch_ticker(self, _symbol):
            return {"last": 120.0}

    engine = SimpleNamespace(db=_DB(), exchange=_Exchange())
    engine._utbreakout_plan_symbol_keys = lambda symbol: [symbol]

    outcome = asyncio.run(record_closed_trade_accounting(
        engine,
        "SNDK/USDT:USDT",
        "scanner position completed",
        state={"_require_exchange_fills": True},
    ))

    assert outcome["status"] == "UNRESOLVED"
    assert engine.db.closed is False


def test_reconciled_close_time_excludes_later_same_symbol_trade_cycle():
    class _Exchange:
        def fetch_my_trades(self, *_args, **_kwargs):
            return [
                {
                    "timestamp": 1_767_229_195_000,
                    "side": "sell",
                    "amount": 1.0,
                    "price": 105.0,
                    "realizedPnl": 5.0,
                    "order": "first-close",
                },
                {
                    "timestamp": 1_767_232_800_000,
                    "side": "sell",
                    "amount": 1.0,
                    "price": 120.0,
                    "realizedPnl": 20.0,
                    "order": "later-close",
                },
            ]

    engine = SimpleNamespace(exchange=_Exchange())
    engine._utbreakout_entry_record_for_symbol = lambda *_args, **_kwargs: SimpleNamespace(
        metadata={},
        take_profit_order_ids=[],
        stop_order_id=None,
    )
    open_trade = {
        "side": "long",
        "entry_price": 100.0,
        "quantity": 1.0,
        "entry_time": "2026-01-01T00:00:00+00:00",
    }

    result = asyncio.run(resolve_closed_trade_accounting(
        engine,
        "SNDK/USDT:USDT",
        open_trade,
        state={"_reconciled_closed_at": "2026-01-01T01:00:00+00:00"},
    ))

    assert result["pnl"] == 5.0
    assert result["exit_price"] == 105.0
    assert len(result["exit_legs"]) == 1


def test_binance_spawned_algo_stop_fill_is_classified_by_client_order_id():
    class _Exchange:
        def fetch_my_trades(self, *_args, **_kwargs):
            return [{
                "timestamp": 1_767_229_195_000,
                "side": "sell",
                "amount": 1.0,
                "price": 95.0,
                "realizedPnl": -5.0,
                "order": "regular-order-created-by-algo",
                "info": {"positionSide": "LONG"},
            }]

        def fetch_order(self, order_id, symbol):
            assert order_id == "regular-order-created-by-algo"
            assert symbol == "BTC/USDT:USDT"
            return {
                "id": order_id,
                "clientOrderId": "utbslslBTCUSDTabc123",
                "reduceOnly": True,
            }

    engine = SimpleNamespace(exchange=_Exchange())
    engine._utbreakout_entry_record_for_symbol = lambda *_args, **_kwargs: SimpleNamespace(
        metadata={},
        take_profit_order_ids=[],
        stop_order_id="4000000000000001",
    )
    open_trade = {
        "side": "long",
        "entry_price": 100.0,
        "quantity": 1.0,
        "entry_time": "2026-01-01T00:00:00+00:00",
    }

    result = asyncio.run(resolve_closed_trade_accounting(
        engine,
        "BTC/USDT:USDT",
        open_trade,
    ))

    assert result["pnl"] == -5.0
    assert result["exit_legs"][0]["label"] == "SL"
    assert result["exit_legs"][0]["client_order_id"].startswith("utbslsl")


def test_incomplete_snapshot_never_closes_local_trade_accounting():
    class _Engine(SignalRuntimeMixin):
        db = object()

    result = SimpleNamespace(
        snapshot_complete=False,
        positions_ok=False,
        positions=[],
        closed_position_symbols=["BLESS/USDT:USDT"],
    )

    assert asyncio.run(_Engine()._account_for_reconciled_flat_trades(result)) == []

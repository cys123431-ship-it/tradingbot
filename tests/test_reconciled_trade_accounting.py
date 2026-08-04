import asyncio
from types import SimpleNamespace

from emas import DBManager, SignalRuntimeMixin


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

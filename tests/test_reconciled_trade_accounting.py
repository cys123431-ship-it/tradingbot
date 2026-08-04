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


def test_reconciliation_accounts_flat_trade_but_skips_live_position():
    class _DB:
        def get_open_trades(self):
            return [
                {"symbol": "BLESS/USDT:USDT"},
                {"symbol": "BICO/USDT:USDT"},
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
                    updated_at="2026-08-03T18:58:36+00:00",
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

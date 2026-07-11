import asyncio

import pytest

from trading_safety.order_state import SQLiteTradingStateStore
from trading_safety.trade_accounting import (
    TradeAccountingFinalizer,
    rebuild_engine_performance_stats,
)


def _trade(**overrides):
    trade = {
        "trade_id": "trade-1",
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "engine": "UTB",
        "gross_pnl_usdt": 10.0,
        "net_pnl_usdt": 10.0,
        "realized_r": 1.0,
        "entry_time": "2026-01-01T00:00:00+00:00",
        "exit_time": "2026-01-01T01:00:00+00:00",
        "provisional": True,
    }
    trade.update(overrides)
    return trade


def test_provisional_trade_can_be_finalized_but_not_downgraded(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    inserted = store.upsert_trade_result(_trade())
    updated = store.upsert_trade_result(
        _trade(
            entry_fee_usdt=1.0,
            exit_fee_usdt=2.0,
            funding_usdt=-0.5,
            net_pnl_usdt=6.5,
            provisional=False,
        )
    )
    ignored = store.upsert_trade_result(_trade(net_pnl_usdt=999.0, provisional=True))
    stale_final = store.upsert_trade_result(
        _trade(net_pnl_usdt=999.0, provisional=False, accounting_revision=0)
    )

    assert inserted.inserted and not inserted.updated
    assert updated.updated and updated.finalized
    assert ignored.updated is False
    assert stale_final.updated is False
    assert store.get_trade_result("trade-1")["net_pnl_usdt"] == 6.5


class AccountingExchange:
    def fetch_my_trades(self, _symbol, _since):
        return [
            {"side": "buy", "fee": {"cost": 1.0}},
            {"side": "sell", "fee": {"cost": 2.0}},
        ]

    def fapiPrivateGetIncome(self, _params):
        return [{"incomeType": "FUNDING_FEE", "income": "-0.5"}]


def test_accounting_finalizer_uses_fees_and_funding(tmp_path):
    async def scenario():
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
        store.upsert_trade_result(_trade())
        final = await TradeAccountingFinalizer(AccountingExchange(), store).finalize_trade("trade-1")
        assert final["provisional"] is False
        assert final["net_pnl_usdt"] == pytest.approx(6.5)
        assert store.get_trade_result("trade-1")["accounting_revision"] == 1
        assert store.get_trade_result("trade-1")["last_accounting_error"] is None

    asyncio.run(scenario())


def test_engine_stats_use_final_trades_only_and_include_cost_burdens():
    stats = rebuild_engine_performance_stats(
        [
            _trade(
                trade_id="a",
                gross_pnl_usdt=10,
                net_pnl_usdt=7,
                entry_fee_usdt=1,
                exit_fee_usdt=1,
                funding_usdt=-1,
                realized_r=1,
                provisional=False,
            ),
            _trade(
                trade_id="b",
                gross_pnl_usdt=-5,
                net_pnl_usdt=-6,
                entry_fee_usdt=0.5,
                exit_fee_usdt=0.5,
                funding_usdt=0,
                realized_r=-0.5,
                provisional=False,
            ),
            _trade(trade_id="pending", net_pnl_usdt=1000, provisional=True),
        ]
    )["UTB"]

    assert stats["trade_count"] == 2
    assert stats["net_profit_usdt"] == 1
    assert stats["fee_burden"] == pytest.approx(0.3)
    assert stats["funding_burden"] == pytest.approx(0.1)
    assert stats["max_drawdown_pct"] > 0

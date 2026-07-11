import emas

from trading_safety.order_state import SQLiteTradingStateStore


def test_live_trade_result_persists_once_and_restores_engine_stats(tmp_path):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    trade = {
        "trade_id": "trade-1",
        "engine": "TREND_CONTINUATION_LONG",
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "exit_time": "2026-07-11T00:00:00+00:00",
        "realized_r": 0.5,
        "net_pnl_usdt": 2.0,
    }
    emas.ENGINE_PERFORMANCE_STATS.clear()

    assert emas.record_live_trade_result(trade, store=store) is True
    assert emas.record_live_trade_result(trade, store=store) is False
    assert emas.ENGINE_PERFORMANCE_STATS[trade["engine"]]["trade_count"] == 1

    emas.ENGINE_PERFORMANCE_STATS.clear()
    assert emas._restore_engine_performance_stats(store) == 1
    assert emas.ENGINE_PERFORMANCE_STATS[trade["engine"]]["expectancy_r"] == 0.5
    emas.ENGINE_PERFORMANCE_STATS.clear()

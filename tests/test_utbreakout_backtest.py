import scripts.utbreakout_backtest as bt


def _row(idx, close):
    return {
        "timestamp": idx * 60_000,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 1000.0,
    }


def test_backtest_closes_open_position_at_end_of_data(monkeypatch):
    rows = [_row(idx, 100.0 + idx) for idx in range(6)]

    def forced_utbot_rows(rows, *, key_value, atr_period):
        atr = [10.0 for _ in rows]
        trail = [50.0 for _ in rows]
        signal = [None for _ in rows]
        bias = ["long" for _ in rows]
        signal[1] = "long"
        return atr, trail, signal, bias

    monkeypatch.setattr(bt, "utbot_rows", forced_utbot_rows)

    result = bt.simulate_utbot_rr(
        rows,
        key_value=2.0,
        atr_period=10,
        initial_balance=1000.0,
        risk_per_trade_percent=0.5,
        max_risk_per_trade_usdt=10.0,
        leverage=5.0,
        stop_atr_multiplier=5.0,
        rr_multiple=2.0,
        partial_take_profit_r_multiple=0.0,
        partial_take_profit_ratio=0.0,
        fee_bps=4.0,
        slippage_bps=1.0,
    )

    assert result["trades"] == 1
    trade = result["trades_detail"][0]
    assert trade["exit_idx"] == len(rows) - 1
    assert trade["exit_reason"] == "END_OF_DATA"
    assert trade["fee_paid"] > 0

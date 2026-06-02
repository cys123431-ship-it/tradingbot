import argparse

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


def test_backtest_applies_time_stop_before_tp1(monkeypatch):
    rows = [_row(idx, 100.0) for idx in range(20)]

    def forced_utbot_rows(rows, *, key_value, atr_period):
        atr = [1.0 for _ in rows]
        trail = [90.0 for _ in rows]
        signal = [None for _ in rows]
        bias = ["long" for _ in rows]
        signal[1] = "long"
        return atr, trail, signal, bias

    monkeypatch.setattr(bt, "utbot_rows", forced_utbot_rows)

    result = bt.simulate_utbot_rr(
        rows,
        key_value=2.0,
        atr_period=10,
        stop_atr_multiplier=20.0,
        rr_multiple=2.0,
        partial_take_profit_r_multiple=1.5,
        partial_take_profit_ratio=0.5,
        exit_policy_name="FIXED_TP_TIME_STOP",
        fee_bps=0.0,
        slippage_bps=0.0,
    )

    assert result["time_stop_count"] == 1
    assert result["trades_detail"][0]["exit_reason"] == "TIME_STOP_NO_FOLLOW_THROUGH"


def test_compare_exit_policies_runs_all_candidates(monkeypatch):
    rows = [_row(idx, 100.0 + idx * 0.1) for idx in range(20)]

    def forced_utbot_rows(rows, *, key_value, atr_period):
        atr = [1.0 for _ in rows]
        trail = [90.0 for _ in rows]
        signal = [None for _ in rows]
        bias = ["long" for _ in rows]
        signal[1] = "long"
        return atr, trail, signal, bias

    monkeypatch.setattr(bt, "utbot_rows", forced_utbot_rows)
    args = argparse.Namespace(
        strategy="live-parity",
        exit_policy="HYBRID_DEFENSIVE",
        timeframe="15m",
        symbol="BTC/USDT",
        initial_balance=1000.0,
        risk_pct=0.5,
        max_risk_usdt=10.0,
        leverage=5.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        funding_bps_per_bar=0.0,
        partial_tp_r=1.5,
        partial_tp_ratio=0.5,
        no_breakeven_after_partial=False,
        trailing_atr_mult=0.0,
        meta_label_gate=False,
        regime_router=False,
        wf_min_trades=1,
    )

    result = bt.compare_exit_policies(rows, {"forced": {"key_value": 2.0, "atr_period": 10}}, args)

    assert set(result["results"]) == set(bt.EXIT_POLICY_CANDIDATES)

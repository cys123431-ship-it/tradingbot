import argparse
import csv

import pytest

import scripts.utbreakout_backtest as bt
from utbreakout.engine_router import LadderTP, TradeDecision


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


def test_backtest_fills_entry_on_next_candle_open(monkeypatch):
    rows = [_row(idx, 100.0) for idx in range(5)]
    rows[1]["close"] = 100.0
    rows[2]["open"] = 110.0
    rows[2]["high"] = 111.0
    rows[2]["low"] = 109.0
    rows[2]["close"] = 110.0

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
        stop_atr_multiplier=10.0,
        rr_multiple=2.0,
        partial_take_profit_r_multiple=0.0,
        partial_take_profit_ratio=0.0,
        fee_bps=0.0,
        slippage_bps=10.0,
    )

    trade = result["trades_detail"][0]
    assert trade["signal_idx"] == 1
    assert trade["entry_idx"] == 2
    assert trade["entry_bar_index"] == 2
    assert trade["entry_fill_price_source"] == "NEXT_OPEN"
    assert trade["entry_price"] == 110.0 * (1.0 + 10.0 / 10000.0)


def test_backtest_skips_signal_without_next_candle(monkeypatch):
    rows = [_row(idx, 100.0 + idx) for idx in range(2)]

    def forced_utbot_rows(rows, *, key_value, atr_period):
        atr = [10.0 for _ in rows]
        trail = [50.0 for _ in rows]
        signal = [None for _ in rows]
        bias = ["long" for _ in rows]
        signal[-1] = "long"
        return atr, trail, signal, bias

    monkeypatch.setattr(bt, "utbot_rows", forced_utbot_rows)

    result = bt.simulate_utbot_rr(
        rows,
        key_value=2.0,
        atr_period=10,
        fee_bps=0.0,
        slippage_bps=0.0,
    )

    assert result["trades"] == 0


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


def test_candidate_comparison_includes_new_alpha_candidates(monkeypatch):
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
        advanced_alpha=False,
        adaptive_ladder_tp=False,
        macro_guard=False,
        engine="ALL",
        enabled_engines=None,
        min_alpha_confidence=0.55,
        include_advanced_alpha=True,
        wf_min_trades=1,
    )

    result = bt.compare_strategy_candidates(rows, {"forced": {"key_value": 2.0, "atr_period": 10}}, args)

    assert "BI_DIRECTIONAL_ALPHA" in result["results"]
    assert "TREND_AND_REVERSAL_COMBO" in result["results"]
    assert "LIQUIDITY_SWEEP_ENGINE" in result["results"]
    assert "AGGRESSIVE_BUT_CAPPED_ALPHA" in result["results"]


def test_backtest_uses_final_trade_decision_and_records_ladder_counts(monkeypatch):
    rows = [
        {"timestamp": idx, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000.0}
        for idx in range(4)
    ]
    rows[1]["high"] = 102.0
    rows[1]["low"] = 100.2

    def forced_utbot_rows(rows, *, key_value, atr_period):
        atr = [1.0 for _ in rows]
        trail = [99.0 for _ in rows]
        signal = [None for _ in rows]
        bias = ["long" for _ in rows]
        return atr, trail, signal, bias

    calls = {"count": 0}

    def forced_decision(candle, context, config, models=None, stats=None):
        calls["count"] += 1
        return TradeDecision(
            True,
            "LONG",
            "TREND_CONTINUATION_LONG",
            0.5,
            "ADAPTIVE_LADDER_TP",
            LadderTP(1.0, 50.0, 1.6, 50.0),
            False,
            "TREND_UP",
            0.7,
            0.4,
            ["TEST_SIGNAL"],
        )

    monkeypatch.setattr(bt, "utbot_rows", forced_utbot_rows)
    monkeypatch.setattr(bt, "evaluate_final_trade_decision", forced_decision)

    result = bt.simulate_utbot_rr(
        rows,
        key_value=2.0,
        atr_period=10,
        stop_atr_multiplier=1.0,
        advanced_alpha_enabled=True,
        adaptive_ladder_tp_enabled=True,
        fee_bps=0.0,
        slippage_bps=0.0,
    )

    assert calls["count"] >= 1
    assert result["trades"] >= 1
    assert result["engine_performance"]["TREND_CONTINUATION_LONG"]["trades"] >= 1
    assert result["ladder_tp1_count"] >= 1
    assert result["ladder_tp2_count"] >= 1


def test_trailing_stop_calculated_at_close_activates_next_bar(monkeypatch):
    rows = [_row(idx, 100.0) for idx in range(6)]
    rows[2].update({"open": 100.0, "high": 161.0, "low": 90.0, "close": 160.0})
    rows[3].update({"open": 160.0, "high": 171.0, "low": 164.0, "close": 170.0})
    rows[4].update({"open": 170.0, "high": 171.0, "low": 167.0, "close": 168.0})

    def forced_utbot_rows(rows, *, key_value, atr_period):
        atr = [2.0 for _ in rows]
        trail = [50.0 for _ in rows]
        signal = [None for _ in rows]
        bias = ["long" for _ in rows]
        signal[0] = "long"
        return atr, trail, signal, bias

    monkeypatch.setattr(bt, "utbot_rows", forced_utbot_rows)
    result = bt.simulate_utbot_rr(
        rows,
        key_value=2.0,
        atr_period=10,
        stop_atr_multiplier=5.0,
        rr_multiple=10.0,
        partial_take_profit_r_multiple=0.5,
        partial_take_profit_ratio=0.5,
        breakeven_after_partial=False,
        trailing_atr_multiplier=1.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        time_stop_enabled=False,
        signal_invalid_exit_enabled=False,
    )

    trade = result["trades_detail"][0]
    assert trade["exit_idx"] == 4
    assert trade["exit_reason"] == "SL"


def test_mark_to_market_drawdown_includes_open_position_loss(monkeypatch):
    rows = [_row(idx, 100.0) for idx in range(5)]
    rows[2].update({"open": 100.0, "high": 101.0, "low": 79.0, "close": 80.0})
    rows[3].update({"open": 100.0, "high": 111.0, "low": 99.0, "close": 110.0})
    rows[4].update({"open": 110.0, "high": 111.0, "low": 109.0, "close": 110.0})

    def forced_utbot_rows(rows, *, key_value, atr_period):
        atr = [10.0 for _ in rows]
        trail = [40.0 for _ in rows]
        signal = [None for _ in rows]
        bias = ["long" for _ in rows]
        signal[0] = "long"
        return atr, trail, signal, bias

    monkeypatch.setattr(bt, "utbot_rows", forced_utbot_rows)
    result = bt.simulate_utbot_rr(
        rows,
        key_value=2.0,
        atr_period=10,
        stop_atr_multiplier=5.0,
        rr_multiple=10.0,
        partial_take_profit_r_multiple=0.0,
        partial_take_profit_ratio=0.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        time_stop_enabled=False,
        signal_invalid_exit_enabled=False,
    )

    assert result["net_pnl"] > 0
    assert result["max_drawdown_usdt"] > 0
    assert any(point["unrealized_pnl"] < 0 for point in result["equity_curve"])


def test_actual_funding_applies_only_on_marked_funding_event(monkeypatch):
    rows = [_row(idx, 100.0) for idx in range(5)]
    for row in rows:
        row["funding_rate"] = 0.001
    rows[2]["funding_event"] = True

    def forced_utbot_rows(rows, *, key_value, atr_period):
        atr = [10.0 for _ in rows]
        trail = [40.0 for _ in rows]
        signal = [None for _ in rows]
        bias = ["long" for _ in rows]
        signal[0] = "long"
        return atr, trail, signal, bias

    monkeypatch.setattr(bt, "utbot_rows", forced_utbot_rows)
    result = bt.simulate_utbot_rr(
        rows,
        key_value=2.0,
        atr_period=10,
        stop_atr_multiplier=5.0,
        rr_multiple=10.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        funding_mode="actual",
        time_stop_enabled=False,
        signal_invalid_exit_enabled=False,
    )

    trade = result["trades_detail"][0]
    expected_once = trade["entry_price"] * trade["initial_qty"] * 0.001
    assert result["funding_mode"] == "actual"
    assert trade["funding_abs"] == pytest.approx(expected_once)


def test_csv_loader_preserves_actual_funding_event_fields(tmp_path):
    path = tmp_path / "ohlcv.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp", "open", "high", "low", "close", "volume",
                "funding_rate", "funding_timestamp", "funding_event",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "timestamp": 1,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 10,
                "funding_rate": 0.001,
                "funding_timestamp": 1,
                "funding_event": "true",
            }
        )

    row = bt.load_ohlcv_csv(path)[0]
    assert row["funding_rate"] == pytest.approx(0.001)
    assert row["funding_timestamp"] == "1"
    assert row["funding_event"] is True


def test_month_to_candle_conversion_respects_timeframe():
    assert bt.candles_for_months(1, "15m") == 2880
    assert bt.candles_for_months(1, "1h") == 720
    assert bt.candles_for_months(1, "4h") == 180
    assert bt.candles_for_months(2, "1d") == 60


def test_month_to_candle_conversion_rejects_unknown_timeframe():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        bt.candles_for_months(1, "banana")


def test_walk_forward_report_purges_boundaries_and_reports_selection_stability(monkeypatch):
    observed_test_starts = []

    def fake_simulate(rows, params, args):
        is_test = len(rows) == 20
        if is_test:
            observed_test_starts.append(rows[0])
        return {
            "trades": 10,
            "profit_factor": 1.20 if is_test else params["profit_factor"],
            "average_R": 0.10 if is_test else params["average_R"],
            "max_drawdown_pct": 1.0,
            "net_pnl": 1.0,
            "trades_detail": [],
        }

    monkeypatch.setattr(bt, "_simulate_variant", fake_simulate)
    args = argparse.Namespace(
        wf_train_candles=50,
        wf_test_candles=20,
        wf_purge_candles=5,
        wf_min_trades=5,
        wf_selection_warning_threshold=0.60,
    )

    report = bt.walk_forward_report(
        list(range(100)),
        {
            "stable": {"profit_factor": 1.50, "average_R": 0.40},
            "weak": {"profit_factor": 1.10, "average_R": 0.10},
        },
        args,
    )

    assert report["window_count"] == 2
    assert observed_test_starts == [55, 75]
    assert report["windows"][0]["purge_start"] == 50
    assert report["windows"][0]["purge_end"] == 54
    assert report["selection_counts"] == {"stable": 2}
    assert report["selection_concentration"] == pytest.approx(1.0)
    assert report["selection_concentration_warning"] is True
    assert report["mean_generalization_gap_r"] == pytest.approx(0.30)


def test_walk_forward_selection_prefers_temporal_stability_over_full_train_peak(monkeypatch):
    def fake_simulate(rows, params, args):
        if len(rows) == 50:
            profit_factor = 2.20 if params["profile"] == "spiky" else 1.55
            average_r = 0.65 if params["profile"] == "spiky" else 0.32
        elif len(rows) == 25 and params["profile"] == "spiky":
            is_early_half = rows[0] < 25
            profit_factor = 3.00 if is_early_half else 0.65
            average_r = 1.10 if is_early_half else -0.35
        elif len(rows) == 25:
            profit_factor = 1.50
            average_r = 0.30
        else:
            profit_factor = 1.20
            average_r = 0.10
        return {
            "trades": 10,
            "profit_factor": profit_factor,
            "average_R": average_r,
            "max_drawdown_pct": 1.0,
            "net_pnl": 1.0,
            "trades_detail": [],
        }

    monkeypatch.setattr(bt, "_simulate_variant", fake_simulate)
    args = argparse.Namespace(
        wf_train_candles=50,
        wf_test_candles=20,
        wf_purge_candles=5,
        wf_min_trades=5,
        wf_selection_warning_threshold=0.60,
        wf_robust_selection=True,
    )

    report = bt.walk_forward_report(
        list(range(75)),
        {
            "spiky": {"profile": "spiky"},
            "stable": {"profile": "stable"},
        },
        args,
    )

    assert report["robust_selection_enabled"] is True
    assert report["windows"][0]["selected"] == "stable"
    assert report["windows"][0]["train_results"]["spiky"]["average_R"] == pytest.approx(0.65)
    assert report["windows"][0]["train_stability_results"]["spiky"]["second_half"]["average_R"] == pytest.approx(-0.35)

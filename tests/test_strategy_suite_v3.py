import pytest

from utbreakout.crowding_unwind import evaluate_crowding_unwind
from utbreakout.qh_flow import evaluate_l2_gate, evaluate_qh_flow
from utbreakout.strategy_allocator import (
    evaluate_strategy_allocation,
    scale_plan_risk,
    summarize_strategy_trades,
)


def _depth(bid_qty=1000, ask_qty=1000, bid=100.0, ask=100.01):
    return {
        "bids": [[bid - i * 0.01, bid_qty] for i in range(20)],
        "asks": [[ask + i * 0.01, ask_qty] for i in range(20)],
    }


def test_dynamic_l2_identifies_bid_support_and_direction_conflict():
    history = [{
        "bid_depth_usdt": 1_000_000,
        "ask_depth_usdt": 2_000_000,
        "imbalance_pct": -33.3,
        "spread_pct": 0.01,
    }]
    result = evaluate_l2_gate(
        _depth(bid_qty=1500, ask_qty=700),
        {"liquidity_high_depth_usdt": 1000},
        history=history,
        side="long",
        symbol="BTC/USDT:USDT",
    )
    assert result["state"] == "bid_support"
    assert result["allowed"] is True
    assert result["risk_multiplier"] == pytest.approx(1.0)

    conflict = evaluate_l2_gate(
        _depth(bid_qty=1500, ask_qty=700),
        {"liquidity_high_depth_usdt": 1000},
        history=history,
        side="short",
        symbol="BTC/USDT:USDT",
    )
    assert conflict["state"] == "bid_support"
    assert conflict["risk_multiplier"] < 1.0


def test_qh_v2_blocks_benchmark_conflict_and_persistence_reversal():
    current = {
        "buy_notional": 120_000,
        "sell_notional": 30_000,
        "total_notional": 150_000,
        "trade_count": 300,
        "imbalance": 0.60,
        "return_pct": 0.06,
        "last_price": 100.06,
    }
    baseline = [
        {"total_notional": 70_000 + i * 1000, "trade_count": 140 + i, "imbalance": 0.0}
        for i in range(8)
    ]
    l2 = {
        "state": "deep_balanced",
        "legacy_state": "calm",
        "allowed": True,
        "risk_multiplier": 1.0,
        "liquidity_tier": "high",
    }
    conflict = evaluate_qh_flow(
        current,
        baseline,
        l2,
        benchmarks={
            "BTC": {"imbalance": -0.20},
            "ETH": {"imbalance": -0.15},
        },
        persistence={"total_notional": 50_000, "imbalance": 0.30, "return_pct": 0.01},
    )
    assert conflict.allowed is False
    assert conflict.reason == "benchmark_direction_conflict"

    reversal = evaluate_qh_flow(
        current,
        baseline,
        l2,
        benchmarks={"BTC": {"imbalance": 0.20}},
        persistence={"total_notional": 50_000, "imbalance": -0.40, "return_pct": -0.05},
    )
    assert reversal.allowed is False
    assert reversal.reason == "flow_not_persistent"


def _crowding_rows(side):
    rows = []
    for idx in range(15):
        close = 100.0 + idx * (0.03 if side == "short" else -0.03)
        rows.append({
            "open": close - 0.02,
            "high": close + 0.25,
            "low": close - 0.25,
            "close": close,
        })
    if side == "short":
        rows[-1] = {"open": 100.1, "high": 100.15, "low": 98.8, "close": 98.9}
    else:
        rows[-1] = {"open": 99.9, "high": 101.3, "low": 99.85, "close": 101.2}
    return rows


def test_funding_oi_crowding_unwind_short_and_long():
    short = evaluate_crowding_unwind(
        _crowding_rows("short"),
        {
            "funding_rate": 0.0012,
            "funding_percentile_30d": 99,
            "open_interest_delta_z": 2.5,
            "open_interest_change_4h": 7.0,
            "long_short_ratio": 2.0,
            "taker_buy_sell_ratio": 1.3,
            "rolling_ofi_score": 20,
        },
        {"allowed": True, "state": "ask_pressure", "direction_support": "short", "risk_multiplier": 1.0},
    )
    assert short.allowed is True
    assert short.side == "short"

    long = evaluate_crowding_unwind(
        _crowding_rows("long"),
        {
            "funding_rate": -0.0012,
            "funding_percentile_30d": 99,
            "open_interest_delta_z": 2.5,
            "open_interest_change_4h": 7.0,
            "long_short_ratio": 0.5,
            "taker_buy_sell_ratio": 0.7,
            "rolling_ofi_score": -20,
        },
        {"allowed": True, "state": "bid_support", "direction_support": "long", "risk_multiplier": 1.0},
    )
    assert long.allowed is True
    assert long.side == "long"


def test_strategy_allocator_is_neutral_for_small_sample_and_reduces_bad_strategy():
    small = summarize_strategy_trades(
        [{"strategy": "qh_flow_v1", "net_r": -1.0, "net_pnl": -1.0}],
        "qh_flow_v1",
    )
    assert evaluate_strategy_allocation(small).multiplier == pytest.approx(1.0)

    rows = []
    for idx in range(20):
        rows.append({
            "strategy": "qh_flow_v1",
            "net_r": -0.5 if idx >= 10 else 0.1,
            "net_pnl": -0.5 if idx >= 10 else 0.1,
            "mfe_r": 1.0,
        })
    metrics = summarize_strategy_trades(rows, "qh_flow_v1")
    allocation = evaluate_strategy_allocation(metrics)
    assert 0.30 <= allocation.multiplier < 1.0
    scaled = scale_plan_risk({"qty": 10, "risk_usdt": 5, "entry_price": 100}, allocation.multiplier)
    assert scaled["qty"] < 10
    assert scaled["entry_price"] == 100

from utbreakout.rspt_v3 import residual_strength_percentiles


def _rows_from_returns(returns, start=100.0):
    rows = [{"timestamp": 0, "close": start, "open": start, "high": start, "low": start}]
    close = start
    for idx, value in enumerate(returns, start=1):
        close *= 1.0 + value
        rows.append({"timestamp": idx * 14_400_000, "close": close, "open": close, "high": close, "low": close})
    return rows


def test_rspt_v3_removes_alt_common_and_volatility_factors():
    count = 130
    btc = [0.001 + ((idx % 5) - 2) * 0.0001 for idx in range(count)]
    eth = [0.0007 + ((idx % 7) - 3) * 0.00008 for idx in range(count)]
    common = [((idx % 9) - 4) * 0.00025 for idx in range(count)]
    rows = {
        "BTC/USDT:USDT": _rows_from_returns(btc),
        "ETH/USDT:USDT": _rows_from_returns(eth),
    }
    symbols = []
    for name, alpha in (("STRONG", 0.0012), ("MID", 0.0001), ("WEAK", -0.0009), ("ALT1", 0), ("ALT2", 0), ("ALT3", 0)):
        symbol = f"{name}/USDT:USDT"
        symbols.append(symbol)
        returns = [0.5 * b + 0.3 * e + c + alpha for b, e, c in zip(btc, eth, common)]
        rows[symbol] = _rows_from_returns(returns)
    result = residual_strength_percentiles(
        symbols,
        rows,
        {
            "relative_strength_reference_symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
            "relative_strength_min_candidates": 4,
            "alt_common_factor_min_symbols": 3,
            "relative_strength_short_lookback_bars": 28,
            "relative_strength_long_lookback_bars": 100,
            "residual_strength_min_aligned_returns": 40,
        },
    )
    assert result["STRONG/USDT:USDT"]["percentile"] > result["MID/USDT:USDT"]["percentile"]
    assert result["MID/USDT:USDT"]["percentile"] > result["WEAK/USDT:USDT"]["percentile"]
    assert result["STRONG/USDT:USDT"]["method"] == "btc_eth_alt_vol_residual_momentum"
    assert "ALT_EQUAL_WEIGHT" in result["STRONG/USDT:USDT"]["factor_names"]


def test_crowding_missing_derivatives_is_not_reported_as_not_extreme():
    decision = evaluate_crowding_unwind(
        _crowding_rows("short"),
        {
            "funding_rate": None,
            "open_interest_delta_z": None,
            "open_interest_change_4h": None,
            "long_short_ratio": None,
        },
        {"allowed": True, "state": "deep_balanced", "risk_multiplier": 1.0},
    )
    assert decision.allowed is False
    assert decision.reason == "crowding_derivatives_data_missing"
    assert decision.metrics["derivatives_data_ready"] is False
    assert "funding_rate" in decision.metrics["missing_derivatives_fields"]
    assert "long_short_ratio" in decision.metrics["missing_derivatives_fields"]


def test_crowding_not_extreme_requires_complete_derivatives_data():
    decision = evaluate_crowding_unwind(
        _crowding_rows("short"),
        {
            "funding_rate": 0.0001,
            "funding_percentile_30d": 50.0,
            "open_interest_delta_z": 0.2,
            "open_interest_change_4h": 0.5,
            "long_short_ratio": 1.1,
        },
        {"allowed": True, "state": "deep_balanced", "risk_multiplier": 1.0},
    )
    assert decision.reason == "crowding_not_extreme"
    assert decision.metrics["derivatives_data_ready"] is True
    assert decision.metrics["missing_derivatives_fields"] == []

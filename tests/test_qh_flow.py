import pytest

from utbreakout.qh_flow import (
    boundary_phase,
    enrich_with_baseline,
    evaluate_l2_gate,
    evaluate_qh_flow,
    quarter_hour_boundary_ms,
    summarize_agg_trades,
)


def _trade(ts, price, qty, buyer_is_maker):
    return {"T": ts, "p": str(price), "q": str(qty), "m": buyer_is_maker}


def test_quarter_hour_boundary_and_phase():
    now = 1_800_000 + 15_000
    assert quarter_hour_boundary_ms(now) == 1_800_000
    assert boundary_phase(now, {"capture_seconds": 10})["phase"] == "ready"


def test_agg_trade_summary_uses_buyer_is_maker_for_taker_side():
    rows = [
        _trade(1, 100, 2, False),
        _trade(2, 101, 1, True),
    ]
    result = summarize_agg_trades(rows)
    assert result["buy_notional"] == pytest.approx(200)
    assert result["sell_notional"] == pytest.approx(101)
    assert result["imbalance"] > 0
    assert result["return_pct"] == pytest.approx(1.0)


def test_l2_gate_classifies_calm_mixed_and_stressed():
    calm = {
        "bids": [[100.00, 1000], [99.99, 1000]],
        "asks": [[100.01, 1000], [100.02, 1000]],
    }
    result = evaluate_l2_gate(calm, {"l2_calm_min_depth_usdt": 1000})
    assert result["state"] == "calm"
    assert result["allowed"] is True

    stressed = {"bids": [[100.0, 1]], "asks": [[101.0, 1]]}
    result = evaluate_l2_gate(stressed)
    assert result["state"] == "stressed"
    assert result["risk_multiplier"] == 0


def test_qh_flow_accepts_strong_long_flow():
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
        {"total_notional": 70_000 + i * 1000, "trade_count": 140 + i, "imbalance": 0.02 * ((i % 3) - 1)}
        for i in range(8)
    ]
    l2 = {"state": "calm", "allowed": True, "risk_multiplier": 1.0}
    decision = evaluate_qh_flow(current, baseline, l2)
    assert decision.allowed is True
    assert decision.side == "long"
    assert decision.risk_multiplier > 0


def test_qh_flow_blocks_opposite_price_retention():
    current = {
        "buy_notional": 120_000,
        "sell_notional": 30_000,
        "total_notional": 150_000,
        "trade_count": 300,
        "imbalance": 0.60,
        "return_pct": -0.02,
    }
    baseline = [
        {"total_notional": 70_000, "trade_count": 140, "imbalance": 0.0}
        for _ in range(8)
    ]
    l2 = {"state": "calm", "allowed": True, "risk_multiplier": 1.0}
    decision = evaluate_qh_flow(current, baseline, l2)
    assert decision.allowed is False
    assert decision.reason == "price_move_not_retained"


def test_baseline_enrichment_is_robust_when_variance_is_zero():
    current = {"total_notional": 200, "trade_count": 20, "imbalance": 0.4}
    base = [{"total_notional": 100, "trade_count": 10, "imbalance": 0.0} for _ in range(4)]
    enriched = enrich_with_baseline(current, base)
    assert enriched["imbalance_z"] > 0
    assert enriched["notional_ratio"] == pytest.approx(2.0)

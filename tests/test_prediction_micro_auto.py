import pytest

from prediction import (
    PREDICTION_STRATEGY_CATALOG,
    PaperLedger,
    PredictAuthRequired,
    PredictClient,
    analyze_orderbook,
    build_prediction_micro_plan,
    default_prediction_micro_config,
    estimate_crypto_up_probability,
    evaluate_prediction_edge,
    normalize_market,
    score_prediction_candidate,
)


def test_prediction_market_normalizer_accepts_crypto_and_macro_only():
    crypto = normalize_market({"id": 1, "title": "Bitcoin Up or Down - May 4", "categorySlug": "crypto"})
    macro = normalize_market({"id": 2, "title": "Fed rate unchanged in June", "categorySlug": "economics"})
    sports = normalize_market({"id": 3, "title": "UEFA Champions League Winner", "categorySlug": "sports"})

    assert crypto["accepted"] is True
    assert crypto["market_type"] == "crypto"
    assert macro["accepted"] is True
    assert macro["market_type"] == "macro"
    assert sports["accepted"] is False
    assert "REJECTED_PREDICTION_CATEGORY" in sports["reject_reasons"]


def test_predict_mainnet_without_api_key_rejects_before_network():
    client = PredictClient.mainnet(api_key="")
    with pytest.raises(PredictAuthRequired) as exc:
        client.get_markets(first=1)
    assert "PREDICTION_MAINNET_AUTH_REQUIRED" in str(exc.value)


def test_orderbook_analyzer_calculates_spread_depth_and_impact():
    analysis = analyze_orderbook(
        {
            "data": {
                "bids": [[0.62, 100], [0.60, 50]],
                "asks": [[0.66, 10], [0.70, 20]],
            }
        },
        spend_usdt=5,
    )

    assert analysis["accepted"] is True
    assert analysis["best_yes_bid"] == 0.62
    assert analysis["best_yes_ask"] == 0.66
    assert round(analysis["yes_spread"], 4) == 0.04
    assert analysis["buy_yes_avg_price"] >= 0.66
    assert analysis["ask_depth_shares"] == 30


def test_probability_edge_must_exceed_fee_spread_and_margin():
    weak = evaluate_prediction_edge(
        fair_probability=0.68,
        market_price=0.66,
        fee_rate_bps=200,
        spread_decimal=0.04,
        safety_margin=0.03,
    )
    strong = evaluate_prediction_edge(
        fair_probability=0.74,
        market_price=0.66,
        fee_rate_bps=200,
        spread_decimal=0.02,
        safety_margin=0.02,
    )

    assert weak["accepted"] is False
    assert weak["reject_code"] == "REJECTED_PREDICTION_EDGE_LOW"
    assert strong["accepted"] is True
    assert strong["edge"] > 0


def test_crypto_up_probability_respects_price_distance():
    up = estimate_crypto_up_probability(
        open_price=100,
        current_price=102,
        minutes_remaining=30,
        realized_vol_pct=3,
    )
    down = estimate_crypto_up_probability(
        open_price=100,
        current_price=98,
        minutes_remaining=30,
        realized_vol_pct=3,
    )

    assert up > 0.5
    assert down < 0.5


def test_prediction_micro_guard_caps_total_at_ten_usdt_and_one_open_position():
    cfg = default_prediction_micro_config()
    market = normalize_market({"id": 1, "title": "Bitcoin Up or Down", "categorySlug": "crypto"})
    accepted = build_prediction_micro_plan(
        market=market,
        side="YES",
        market_price=0.55,
        edge=0.08,
        total_allocated_usdt=9.0,
        open_position_count=0,
        cfg=cfg,
    )
    capped = build_prediction_micro_plan(
        market=market,
        side="YES",
        market_price=0.55,
        edge=0.08,
        total_allocated_usdt=10.0,
        open_position_count=0,
        cfg=cfg,
    )
    open_limit = build_prediction_micro_plan(
        market=market,
        side="YES",
        market_price=0.55,
        edge=0.08,
        total_allocated_usdt=0.0,
        open_position_count=1,
        cfg=cfg,
    )

    assert accepted["accepted"] is True
    assert accepted["stake_usdt"] == 1.0
    assert capped["reject_code"] == "REJECTED_PREDICTION_EQUITY_CAP"
    assert open_limit["reject_code"] == "REJECTED_PREDICTION_OPEN_POSITION_LIMIT"


def test_prediction_paper_ledger_tracks_close_settlement_brier_and_pnl():
    ledger = PaperLedger()
    plan = {
        "market_id": 1,
        "market_title": "Bitcoin Up or Down",
        "market_type": "crypto",
        "side": "YES",
        "stake_usdt": 1.0,
        "entry_price": 0.50,
        "shares": 2.0,
    }
    pos = ledger.open_position(plan, fair_probability=0.65, opened_at="2026-05-04T00:00:00Z")
    settled = ledger.settle_position(pos["id"], outcome_won=True, closing_price=0.75)
    summary = ledger.summary()

    assert settled["pnl_usdt"] == 1.0
    assert round(settled["brier_score"], 4) == 0.1225
    assert summary["closed_positions"] == 1
    assert summary["realized_pnl_usdt"] == 1.0


def test_prediction_strategy_catalog_and_candidate_score_are_paper_only():
    assert len(PREDICTION_STRATEGY_CATALOG) == 100
    assert all(item["paper_only"] for item in PREDICTION_STRATEGY_CATALOG)

    market = normalize_market({"id": 1, "title": "Bitcoin Up or Down", "categorySlug": "crypto"})
    orderbook = analyze_orderbook({"data": {"bids": [[0.5, 100]], "asks": [[0.52, 100]]}})
    edge = evaluate_prediction_edge(
        fair_probability=0.62,
        market_price=0.52,
        fee_rate_bps=100,
        spread_decimal=0.02,
        safety_margin=0.02,
    )
    plan = build_prediction_micro_plan(market=market, side="YES", market_price=0.52, edge=edge["edge"])
    scored = score_prediction_candidate(market, orderbook, edge, plan)

    assert scored["accepted"] is True
    assert scored["score"] >= 60


def test_futures_prediction_research_sets_are_not_live_selectable():
    import emas

    assert emas.UTBREAKOUT_ACTIVE_SET_MAX == 50
    for set_id in range(51, 61):
        info = emas.UTBREAKOUT_SET_REGISTRY[set_id]
        assert info["status"] == "planned"
        assert info["params"].get("research_only") is True

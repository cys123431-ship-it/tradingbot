from pathlib import Path

import pytest

from prediction import (
    PREDICTION_STRATEGY_CATALOG,
    PaperLedger,
    PredictAuthRequired,
    PredictClient,
    PredictionLiveCredentials,
    PredictionLiveOrderError,
    analyze_orderbook,
    build_live_market_order_payload,
    build_prediction_micro_plan,
    check_live_preflight,
    default_prediction_micro_config,
    evaluate_paper_position_exit,
    estimate_crypto_up_probability,
    evaluate_prediction_edge,
    extract_market_resolution,
    format_prediction_research_report,
    normalize_market,
    score_prediction_candidate,
    submit_live_market_order,
    provider_label,
    yes_token_id_from_market,
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


def test_predict_mainnet_create_order_requires_jwt():
    client = PredictClient.mainnet(api_key="key", jwt_token="")
    with pytest.raises(PredictAuthRequired) as exc:
        client.create_order({"data": {}})
    assert "PREDICTION_MAINNET_JWT_REQUIRED" in str(exc.value)


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


def test_prediction_live_config_unlocks_paper_only_flag():
    cfg = default_prediction_micro_config()
    cfg["live_enabled"] = True
    plan = build_prediction_micro_plan(
        market=normalize_market({"id": 1, "title": "Bitcoin Up or Down", "categorySlug": "crypto"}),
        side="YES",
        market_price=0.50,
        edge=0.08,
        cfg=cfg,
    )

    assert plan["accepted"] is True
    assert plan["paper_only"] is False
    assert plan["live_enabled"] is True


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


def test_prediction_paper_ledger_persists_to_json(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = PaperLedger()
    plan = {
        "market_id": 7,
        "market_title": "Fed rate unchanged",
        "market_type": "macro",
        "side": "YES",
        "stake_usdt": 1.0,
        "entry_price": 0.25,
        "shares": 4.0,
    }
    ledger.open_position(plan, fair_probability=0.55, opened_at="2026-05-04T00:00:00Z")
    ledger.save(path)

    loaded = PaperLedger.load(path)

    assert loaded.open_positions()[0]["market_id"] == 7
    assert loaded.summary()["open_positions"] == 1
    assert loaded.opened_count_since(hours=24, now="2026-05-04T12:00:00Z") == 1
    assert loaded.opened_count_since(hours=1, now="2026-05-04T12:00:00Z") == 0


def test_prediction_lifecycle_closes_on_edge_loss_and_settles_resolution():
    position = {
        "id": "paper-1",
        "status": "OPEN",
        "opened_at": "2026-05-04T00:00:00Z",
        "side": "YES",
        "entry_price": 0.55,
        "stake_usdt": 1.0,
        "shares": 1.8181818,
    }
    close = evaluate_paper_position_exit(
        position,
        orderbook={"best_yes_bid": 0.54, "yes_mid": 0.55},
        edge_result={"edge": -0.01},
        now="2026-05-04T01:00:00Z",
    )
    settle = evaluate_paper_position_exit(
        position,
        raw_market={"resolution": "YES"},
        orderbook={"best_yes_bid": 0.90},
    )

    assert close["action"] == "close"
    assert close["reason"] == "PAPER_CLOSE_EDGE_GONE"
    assert settle["action"] == "settle"
    assert settle["outcome_won"] is True
    assert extract_market_resolution({"winningOutcome": "No"})["winning_side"] == "NO"


class _FakeAmounts:
    price_per_share = 500000000000000000
    maker_amount = 1000000000000000000
    taker_amount = 2000000000000000000


class _FakeSignedOrder:
    def __init__(self):
        self.salt = "1"
        self.maker = "0xmaker"
        self.signer = "0xsigner"
        self.taker = "0x0000000000000000000000000000000000000000"
        self.token_id = "123"
        self.maker_amount = "1000000000000000000"
        self.taker_amount = "2000000000000000000"
        self.expiration = "1"
        self.nonce = "0"
        self.fee_rate_bps = "100"
        self.side = 0
        self.signature_type = 0
        self.signature = "0xsig"


class _FakeBuilder:
    @classmethod
    def make(cls, *args, **kwargs):
        return cls()

    def get_market_order_amounts(self, *args, **kwargs):
        return _FakeAmounts()

    def balance_of(self, *args, **kwargs):
        return 2 * 10**18

    def build_order(self, *args, **kwargs):
        return {"order": True}

    def build_typed_data(self, *args, **kwargs):
        return {"typed": True}

    def sign_typed_data_order(self, *args, **kwargs):
        return _FakeSignedOrder()

    def build_typed_data_hash(self, *args, **kwargs):
        return "0xhash"


class _FakeSdk:
    class ChainId:
        BNB_MAINNET = 56
        BNB_TESTNET = 97

    class Side:
        BUY = 0

    class OrderBuilderOptions:
        def __init__(self, predict_account=None):
            self.predict_account = predict_account

    class MarketHelperValueInput:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class BuildOrderInput:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Book:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    OrderBuilder = _FakeBuilder


class _FakeLiveClient:
    def __init__(self):
        self.payload = None

    def create_order(self, payload):
        self.payload = payload
        return {"success": True, "data": {"orderId": "ord-1", "orderHash": "0xhash"}}


def test_prediction_live_order_requires_env_unlock_and_builds_payload():
    market = normalize_market({
        "id": 1,
        "title": "Bitcoin Up or Down",
        "categorySlug": "crypto",
        "feeRateBps": 100,
        "outcomes": [{"name": "Yes", "onChainId": "123"}],
    })
    orderbook = {"data": {"marketId": 1, "asks": [[0.50, 10]], "bids": [[0.49, 10]]}}
    locked = PredictionLiveCredentials(api_key="k", jwt_token="j", private_key="p", env_live_enabled=False)
    unlocked = PredictionLiveCredentials(
        api_key="k",
        jwt_token="j",
        private_key="p",
        env_live_enabled=True,
        approvals_confirmed=True,
    )

    assert yes_token_id_from_market(market) == "123"
    with pytest.raises(PredictionLiveOrderError):
        build_live_market_order_payload(
            market=market,
            orderbook_payload=orderbook,
            stake_usdt=1.0,
            credentials=locked,
            sdk_module=_FakeSdk,
        )

    payload = build_live_market_order_payload(
        market=market,
        orderbook_payload=orderbook,
        stake_usdt=1.0,
        credentials=unlocked,
        sdk_module=_FakeSdk,
    )

    assert payload["data"]["strategy"] == "MARKET"
    assert payload["data"]["order"]["hash"] == "0xhash"
    assert payload["data"]["order"]["tokenId"] == "123"


def test_prediction_live_order_submit_uses_client_create_order():
    market = normalize_market({
        "id": 1,
        "title": "Bitcoin Up or Down",
        "categorySlug": "crypto",
        "outcomes": [{"name": "Yes", "onChainId": "123"}],
    })
    client = _FakeLiveClient()
    result = submit_live_market_order(
        client=client,
        market=market,
        orderbook_payload={"data": {"marketId": 1, "asks": [[0.50, 10]], "bids": [[0.49, 10]]}},
        plan={"stake_usdt": 1.0},
        cfg={"live_slippage_bps": 25},
        credentials=PredictionLiveCredentials(
            api_key="k",
            jwt_token="j",
            private_key="p",
            env_live_enabled=True,
            approvals_confirmed=True,
        ),
        sdk_module=_FakeSdk,
    )

    assert result["accepted"] is True
    assert result["order_id"] == "ord-1"
    assert client.payload["data"]["slippageBps"] == "25"


def test_prediction_live_preflight_rejects_unconfirmed_approvals_and_low_balance():
    market = normalize_market({
        "id": 1,
        "title": "Bitcoin Up or Down",
        "categorySlug": "crypto",
        "outcomes": [{"name": "Yes", "onChainId": "123"}],
    })
    orderbook = {"data": {"marketId": 1, "asks": [[0.50, 10]], "bids": [[0.49, 10]]}}

    blocked = check_live_preflight(
        market=market,
        orderbook_payload=orderbook,
        plan={"stake_usdt": 1.0},
        cfg=default_prediction_micro_config(),
        credentials=PredictionLiveCredentials(api_key="k", jwt_token="j", private_key="p", env_live_enabled=True),
        sdk_module=_FakeSdk,
    )

    assert blocked["accepted"] is False
    assert "PREDICTION_APPROVALS_NOT_CONFIRMED" in blocked["reject_reasons"]

    class _LowBalanceBuilder(_FakeBuilder):
        def balance_of(self, *args, **kwargs):
            return int(0.5 * 10**18)

    class _LowBalanceSdk(_FakeSdk):
        OrderBuilder = _LowBalanceBuilder

    low_balance = check_live_preflight(
        market=market,
        orderbook_payload=orderbook,
        plan={"stake_usdt": 1.0},
        cfg=default_prediction_micro_config(),
        credentials=PredictionLiveCredentials(
            api_key="k",
            jwt_token="j",
            private_key="p",
            env_live_enabled=True,
            approvals_confirmed=True,
        ),
        sdk_module=_LowBalanceSdk,
    )

    assert low_balance["accepted"] is False
    assert "PREDICTION_BALANCE_LOW" in low_balance["reject_reasons"]
    assert provider_label() == "Binance Wallet Prediction (Predict.fun API)"


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


def test_prediction_research_report_includes_core_metrics():
    text = format_prediction_research_report(
        {
            "realized_pnl_usdt": 0.25,
            "closed_positions": 2,
            "open_positions": 1,
            "avg_brier_score": 0.18,
            "avg_closing_line_value": 0.04,
            "by_category": {"crypto": {"count": 2, "pnl_usdt": 0.25}},
        },
        reject_counts={"REJECTED_PREDICTION_EDGE_LOW": 3},
        strategy_counts={51: 2},
    )

    assert "Avg Brier" in text
    assert "REJECTED_PREDICTION_EDGE_LOW" in text
    assert "Strategy 51" in text


def test_futures_prediction_sets_are_live_selectable_with_required_filters():
    text = Path("emas.py").read_text(encoding="utf-8")

    assert "UTBREAKOUT_ACTIVE_SET_MAX = 60" in text
    for set_id in range(51, 61):
        start = text.find(f"({set_id}, 'Prediction Futures'")
        assert start >= 0
        end = text.find(f"({set_id + 1},", start) if set_id < 60 else text.find("\n    ]", start)
        segment = text[start:end]
        assert "'research_only': True" not in segment
        assert "prediction_" in segment
    for filter_name in (
        "prediction_orderflow_imbalance",
        "prediction_oi_funding_crowding",
        "prediction_liquidation_cascade",
        "prediction_odds_divergence",
        "prediction_macro_event_guard",
        "prediction_volatility_forecast",
        "prediction_spread_depth_guard",
        "prediction_basis_divergence",
        "prediction_probability_trail",
        "prediction_cross_market_confirmation",
    ):
        assert f"filter_name == '{filter_name}'" in text

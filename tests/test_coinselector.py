from utbreakout.coinselector import (
    build_base_candidate,
    build_selection_report,
    default_coin_selector_config,
    finalize_candidate,
    market_is_tradifi_perpetual,
    normalize_custom_symbols,
    rank_candidates,
    score_selection_quality,
)


def _market(**overrides):
    base = {
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "active": True,
        "type": "swap",
        "info": {"contractType": "PERPETUAL", "status": "TRADING"},
    }
    base.update(overrides)
    return base


def _ticker(**overrides):
    base = {
        "quoteVolume": 250_000_000,
        "percentage": 2.5,
        "count": 120_000,
        "bid": 100.0,
        "ask": 100.02,
    }
    base.update(overrides)
    return base


def _auto_scores(**overrides):
    scores = {
        "trend_score": 70.0,
        "chop_score": 30.0,
        "volatility_score": 76.0,
        "breakout_score": 68.0,
        "momentum_score": 64.0,
        "flow_score": 58.0,
        "alignment_score": 72.0,
        "mtf_momentum_score": 66.0,
        "mtf_volatility_score": 74.0,
        "auto_final_set_id": 22,
        "auto_selected_score": 72.0,
        "auto_score_margin": 9.0,
        "auto_confidence": "normal",
    }
    scores.update(overrides)
    return {"scores": scores}


def test_coinselector_rejects_low_volume_and_non_usdt_perpetual():
    cfg = default_coin_selector_config()
    low_volume = build_base_candidate("ABC/USDT:USDT", _ticker(quoteVolume=50_000_000), _market(), cfg)
    coin_margin = build_base_candidate("BTC/USDC:USDC", _ticker(), _market(quote="USDC", settle="USDC"), cfg)

    assert low_volume["accepted"] is False
    assert "REJECTED_VOLUME_LOW" in low_volume["reject_reasons"]
    assert coin_margin["accepted"] is False
    assert "REJECTED_NOT_USDT_PERPETUAL_TRADING" in coin_margin["reject_reasons"]


def test_coinselector_rejects_default_excluded_sector():
    cfg = default_coin_selector_config()
    candidate = build_base_candidate("DOGE/USDT:USDT", _ticker(quoteVolume=500_000_000), _market(), cfg)

    assert candidate["accepted"] is False
    assert "REJECTED_EXCLUDED_SECTOR" in candidate["reject_reasons"]


def test_coinselector_accepts_tradifi_usdt_perpetual():
    cfg = default_coin_selector_config()
    candidate = build_base_candidate(
        "EWY/USDT:USDT",
        _ticker(quoteVolume=500_000_000),
        _market(info={"contractType": "TRADIFI_PERPETUAL", "status": "TRADING"}),
        cfg,
    )

    assert candidate["accepted"] is True
    assert candidate["tradifi_perpetual"] is True
    assert market_is_tradifi_perpetual("EWY/USDT:USDT", _market(info={"contractType": "TRADIFI_PERPETUAL", "status": "TRADING"})) is True
    assert "REJECTED_NOT_USDT_PERPETUAL_TRADING" not in candidate["reject_reasons"]


def test_custom_symbols_normalize_and_dedupe_to_usdt_pairs():
    symbols = normalize_custom_symbols("BTC BTCUSDT BTC/USDT BTC/USDT:USDT eth, SOL")

    assert symbols == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def test_custom_discovery_relax_only_volume_and_trade_count():
    strict_cfg = default_coin_selector_config()
    relaxed_cfg = dict(strict_cfg)
    relaxed_cfg["min_quote_volume_usdt"] = 0.0
    relaxed_cfg["min_trade_count"] = 0

    strict = build_base_candidate("ABC/USDT:USDT", _ticker(quoteVolume=1_000, count=5), _market(), strict_cfg)
    relaxed = build_base_candidate("ABC/USDT:USDT", _ticker(quoteVolume=1_000, count=5), _market(), relaxed_cfg)
    non_usdt = build_base_candidate("ABC/USDC:USDC", _ticker(quoteVolume=1_000, count=5), _market(quote="USDC", settle="USDC"), relaxed_cfg)
    blacklisted = build_base_candidate(
        "ABC/USDT:USDT",
        _ticker(quoteVolume=1_000, count=5),
        _market(),
        {**relaxed_cfg, "blacklist": ["ABC/USDT"]},
    )
    wide_spread = build_base_candidate("ABC/USDT:USDT", _ticker(quoteVolume=1_000, count=5, ask=101.0), _market(), relaxed_cfg)

    assert strict["accepted"] is False
    assert "REJECTED_VOLUME_LOW" in strict["reject_reasons"]
    assert relaxed["accepted"] is True
    assert non_usdt["accepted"] is False
    assert "REJECTED_NOT_USDT_PERPETUAL_TRADING" in non_usdt["reject_reasons"]
    assert blacklisted["accepted"] is False
    assert "REJECTED_BLACKLIST" in blacklisted["reject_reasons"]
    assert wide_spread["accepted"] is False
    assert "REJECTED_SPREAD_WIDE" in wide_spread["reject_reasons"]


def test_coinselector_scores_utbreakout_set_and_adaptive_tf():
    cfg = default_coin_selector_config()
    candidate = build_base_candidate("BTC/USDT:USDT", _ticker(quoteVolume=900_000_000), _market(), cfg)
    result = finalize_candidate(
        candidate,
        auto_analysis=_auto_scores(),
        selected_set_id=22,
        selected_set_info={"name": "UT + Donchian 20", "family": "Breakout"},
        adaptive_decision={"selected_tf": "30m", "selected_score": 76.0, "decision": "SELECTED"},
        futures_context={"funding_rate": 0.0001, "open_interest_usdt": 1_200_000_000},
        cfg=cfg,
    )

    assert result["score"] >= cfg["min_final_score"]
    assert result["auto_set_id"] == 22
    assert result["adaptive_tf"] == "30m"
    assert result["component_scores"]["utbreakout_regime"] > 15
    assert "selection_quality" in result["component_scores"]


def test_coinselector_penalizes_no_trade_adaptive_tf():
    cfg = default_coin_selector_config()
    candidate = build_base_candidate("ETH/USDT:USDT", _ticker(quoteVolume=900_000_000), _market(), cfg)
    selected = finalize_candidate(
        candidate,
        auto_analysis=_auto_scores(auto_final_set_id=22),
        selected_set_id=22,
        selected_set_info={"name": "UT + Donchian 20", "family": "Breakout"},
        adaptive_decision={"selected_tf": "30m", "selected_score": 76.0, "decision": "SELECTED"},
        cfg=cfg,
    )
    no_trade = finalize_candidate(
        candidate,
        auto_analysis=_auto_scores(auto_final_set_id=22),
        selected_set_id=22,
        selected_set_info={"name": "UT + Donchian 20", "family": "Breakout"},
        adaptive_decision={"selected_tf": None, "selected_score": 0.0, "decision": "NO_TRADE"},
        cfg=cfg,
    )

    assert selected["score"] > no_trade["score"]
    assert "ADAPTIVE_NO_TRADE" in no_trade["soft_warnings"]


def test_coinselector_report_detects_set_concentration():
    cfg = default_coin_selector_config()
    candidates = []
    for idx, symbol in enumerate(["BTC", "ETH", "SOL", "XRP"]):
        base = build_base_candidate(f"{symbol}/USDT:USDT", _ticker(quoteVolume=500_000_000 + idx), _market(), cfg)
        candidates.append(
            finalize_candidate(
                base,
                auto_analysis=_auto_scores(auto_final_set_id=7),
                selected_set_id=7,
                selected_set_info={"name": "UT + ADX + DMI", "family": "Trend Strength"},
                adaptive_decision={"selected_tf": "15m", "selected_score": 70.0, "decision": "SELECTED"},
                cfg=cfg,
            )
        )

    report = build_selection_report(candidates, [], top_n=4)
    ranked = rank_candidates(candidates, top_n=4)

    assert len(ranked) == 4
    assert report["concentration_warning"]["value"] == 7
    assert report["concentration_warning"]["share_pct"] == 100.0


def test_coinselector_ranking_uses_ev_edge_and_excludes_non_actionable_candidates():
    ranked = rank_candidates(
        [
            {
                "symbol": "LEGACY/USDT",
                "accepted": True,
                "score": 92.0,
                "rolling_sharpe": 2.0,
                "quote_volume": 1_000_000_000,
                "ev_allowed": True,
                "ev_net_edge_r": 0.09,
            },
            {
                "symbol": "EDGE/USDT",
                "accepted": True,
                "score": 72.0,
                "rolling_sharpe": 1.0,
                "quote_volume": 100_000_000,
                "ev_allowed": True,
                "ev_net_edge_r": 0.31,
            },
            {
                "symbol": "BLOCKED/USDT",
                "accepted": True,
                "score": 99.0,
                "ev_allowed": False,
                "ev_net_edge_r": 0.50,
            },
        ],
        top_n=3,
    )

    assert [item["symbol"] for item in ranked] == ["EDGE/USDT", "LEGACY/USDT"]


def test_coinselector_selection_quality_rewards_persistent_implementable_momentum():
    cfg = default_coin_selector_config()
    candidate = build_base_candidate("BTC/USDT:USDT", _ticker(quoteVolume=900_000_000), _market(), cfg)
    strong = finalize_candidate(
        candidate,
        auto_analysis=_auto_scores(dominant_side="long"),
        selected_set_id=22,
        selected_set_info={"name": "UT + Donchian 20", "family": "Breakout"},
        adaptive_decision={"selected_tf": "30m", "selected_score": 76.0, "decision": "SELECTED"},
        selection_metrics={
            "rolling_sharpe": 1.1,
            "momentum_consistency": 0.68,
            "directional_efficiency": 0.55,
            "realized_vol_pct": 0.70,
            "return_lookback_pct": 6.0,
            "max_drawdown_pct": 3.5,
            "max_rebound_pct": 4.0,
            "cross_sectional_dispersion_pct": 3.0,
        },
        cfg=cfg,
    )
    weak = finalize_candidate(
        candidate,
        auto_analysis=_auto_scores(dominant_side="long"),
        selected_set_id=22,
        selected_set_info={"name": "UT + Donchian 20", "family": "Breakout"},
        adaptive_decision={"selected_tf": "30m", "selected_score": 76.0, "decision": "SELECTED"},
        selection_metrics={
            "rolling_sharpe": -0.4,
            "momentum_consistency": 0.42,
            "directional_efficiency": 0.08,
            "realized_vol_pct": 3.0,
            "return_lookback_pct": -14.0,
            "max_drawdown_pct": 24.0,
            "max_rebound_pct": 18.0,
            "cross_sectional_dispersion_pct": 14.0,
        },
        cfg=cfg,
    )

    assert strong["component_scores"]["selection_quality"] > weak["component_scores"]["selection_quality"]
    assert strong["score"] > weak["score"]
    assert "SELECTION_DRAWDOWN_RISK" in weak["soft_warnings"]
    assert "SELECTION_HIGH_DISPERSION" in weak["soft_warnings"]


def test_coinselector_selection_quality_penalizes_short_rebound_risk():
    cfg = default_coin_selector_config()
    candidate = build_base_candidate("ETH/USDT:USDT", _ticker(quoteVolume=900_000_000), _market(), cfg)

    calm_short = score_selection_quality(
        {
            **candidate,
            "selection_metrics": {
                "rolling_sharpe": 0.9,
                "momentum_consistency": 0.64,
                "directional_efficiency": 0.48,
                "realized_vol_pct": 0.7,
                "return_lookback_pct": -5.0,
                "max_drawdown_pct": 6.0,
                "max_rebound_pct": 4.0,
            },
        },
        {"dominant_side": "short"},
        cfg,
    )
    rebound_short = score_selection_quality(
        {
            **candidate,
            "selection_metrics": {
                "rolling_sharpe": 0.9,
                "momentum_consistency": 0.64,
                "directional_efficiency": 0.48,
                "realized_vol_pct": 0.7,
                "return_lookback_pct": -5.0,
                "max_drawdown_pct": 6.0,
                "max_rebound_pct": 18.0,
            },
        },
        {"dominant_side": "short"},
        cfg,
    )

    assert calm_short > rebound_short

from utbreakout.regime import classify_market_regime, classify_regime, regime_action


def test_regime_scores_trending_market_high_for_aligned_long():
    result = classify_market_regime(
        {
            "adx": 34,
            "chop": 36,
            "atr_pct": 0.8,
            "hurst_exponent": 0.61,
            "trend_slope_pct": 0.22,
            "momentum_12_pct": 1.8,
            "btc_direction": "bullish",
        },
        side="long",
    )

    assert result["regime"] == "bull_trend"
    assert result["regime_score"] >= 58
    assert result["risk_multiplier"] == 1.0


def test_regime_classifies_choppy_market_and_reduces_risk():
    result = classify_market_regime(
        {
            "adx": 12,
            "chop": 66,
            "atr_pct": 0.45,
            "bb_width_pct": 0.55,
            "keltner_width_pct": 0.50,
            "hurst_exponent": 0.47,
        },
        side="long",
    )

    assert result["regime"] == "choppy"
    assert result["risk_multiplier"] < 0.6


def test_regime_high_volatility_chaos_reduces_risk():
    result = classify_market_regime(
        {
            "adx": 18,
            "chop": 59,
            "atr_pct": 3.0,
            "range_expansion_ratio": 2.4,
            "volume_ratio": 2.1,
        },
        side="short",
    )

    assert result["regime"] == "high_vol_chaos"
    assert result["risk_multiplier"] <= 0.35


def test_regime_router_blocks_chop_and_crowding():
    assert classify_regime({"adx": 10, "atr_percentile": 50, "squeeze_percentile": 50}) == "CHOP"
    assert regime_action("CHOP", {"side": "long"}).allow_long is False
    assert classify_regime({"derivatives_crowding_score": 3}) == "CROWDING_OVERHEATED"
    assert regime_action("CROWDING_OVERHEATED", {"side": "long"}).risk_multiplier == 0.0


def test_regime_router_allows_directional_trends_only():
    up = classify_regime({"adx": 30, "htf_trend": "UP", "plus_di": 25, "minus_di": 10})
    down = classify_regime({"adx": 30, "htf_trend": "DOWN", "plus_di": 10, "minus_di": 25})

    assert up == "TREND_UP"
    assert regime_action(up, {"side": "long"}).allow_long is True
    assert regime_action(up, {"side": "short"}).allow_short is False
    assert down == "TREND_DOWN"
    assert regime_action(down, {"side": "short"}).allow_short is True


def test_regime_router_reduces_risk_in_high_vol_expansion():
    regime = classify_regime({"atr_percentile": 90})
    action = regime_action(regime, {"side": "long"})

    assert regime == "HIGH_VOL_EXPANSION"
    assert action.risk_multiplier == 0.5

from utbreakout.regime import classify_market_regime


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

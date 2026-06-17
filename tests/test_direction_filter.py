from utbreakout.direction_filter import decide_direction


def test_bullish_regime_allows_long_blocks_short():
    decision = decide_direction(
        btc_4h={"close": 105, "ema_fast": 103, "ema_slow": 100, "ema_slope": 0.1},
        btc_1d={"close": 110, "ema_fast": 105, "ema_slow": 100, "ema_slope": 0.1},
        symbol_1h={"close": 55, "ema_fast": 53, "ema_slow": 50, "ema_slope": 0.1},
        entry_15m={"volume_ratio": 0.8, "quality_score": 60},
    )
    assert decision.regime == "bullish"
    assert decision.long_allowed is True
    assert decision.short_allowed is False
    assert decision.size_multiplier == 1.0


def test_bearish_regime_allows_short_only():
    decision = decide_direction(
        btc_4h={"close": 95, "ema_fast": 97, "ema_slow": 100, "ema_slope": -0.1},
        btc_1d={"close": 98, "ema_fast": 99, "ema_slow": 100, "ema_slope": -0.1},
        symbol_1h={"close": 45, "ema_fast": 47, "ema_slow": 50, "ema_slope": -0.1},
        entry_15m={"volume_ratio": 0.8, "quality_score": 60},
        side_hint="short",
    )
    assert decision.regime == "bearish"
    assert decision.long_allowed is False
    assert decision.short_allowed is True


def test_weak_volume_reduces_size_not_block():
    decision = decide_direction(
        btc_4h={"close": 105, "ema_fast": 103, "ema_slow": 100, "ema_slope": 0.1},
        btc_1d={"close": 110, "ema_fast": 105, "ema_slow": 100, "ema_slope": 0.1},
        symbol_1h={"close": 55, "ema_fast": 53, "ema_slow": 50, "ema_slope": 0.1},
        entry_15m={"volume_ratio": 0.50, "quality_score": 60},
        side_hint="long",
    )
    assert decision.long_allowed is True
    assert decision.size_multiplier == 0.5


def test_direction_filter_does_not_ignore_zero_quality_score():
    decision = decide_direction(
        btc_4h={"close": 105, "ema_fast": 103, "ema_slow": 100, "ema_slope": 0.1},
        btc_1d={"close": 110, "ema_fast": 105, "ema_slow": 100, "ema_slope": 0.1},
        symbol_1h={"close": 55, "ema_fast": 53, "ema_slow": 50, "ema_slope": 0.1},
        entry_15m={
            "volume_ratio": 0.8,
            "quality_score": 0,
            "quality_score_v2": 80,
        },
        side_hint="long",
    )
    assert decision.long_allowed is False
    assert decision.size_multiplier == 0.0


def test_direction_filter_weak_volume_reduces_size_not_block():
    decision = decide_direction(
        btc_4h={"close": 105, "ema_fast": 103, "ema_slow": 100, "ema_slope": 0.1},
        btc_1d={"close": 110, "ema_fast": 105, "ema_slow": 100, "ema_slope": 0.1},
        symbol_1h={"close": 55, "ema_fast": 53, "ema_slow": 50, "ema_slope": 0.1},
        entry_15m={"volume_ratio": 0.50, "quality_score": 60},
        side_hint="long",
    )
    assert decision.long_allowed is True
    assert decision.size_multiplier == 0.5


def test_extreme_volume_blocks():
    decision = decide_direction(
        btc_4h={"close": 105, "ema_fast": 103, "ema_slow": 100, "ema_slope": 0.1},
        btc_1d={"close": 110, "ema_fast": 105, "ema_slow": 100, "ema_slope": 0.1},
        symbol_1h={"close": 55, "ema_fast": 53, "ema_slow": 50, "ema_slope": 0.1},
        entry_15m={"volume_ratio": 0.20, "quality_score": 60},
        side_hint="long",
    )
    assert decision.long_allowed is False
    assert decision.short_allowed is False
    assert decision.size_multiplier == 0.0


def test_quality_score_zero_handling():
    # Test that quality_score = 0.0 is treated as a valid score, which is < 20 and should block trades (size_multiplier = 0.0)
    decision = decide_direction(
        btc_4h={"close": 105, "ema_fast": 103, "ema_slow": 100, "ema_slope": 0.1},
        btc_1d={"close": 110, "ema_fast": 105, "ema_slow": 100, "ema_slope": 0.1},
        symbol_1h={"close": 55, "ema_fast": 53, "ema_slow": 50, "ema_slope": 0.1},
        entry_15m={"volume_ratio": 0.8, "quality_score": 0.0},
    )
    # Since quality_score is 0.0 < 20, it should block (long_allowed = False, size_multiplier = 0.0)
    assert decision.long_allowed is False
    assert decision.size_multiplier == 0.0

    # Test that None/missing quality_score defaults to quality unknown (size_multiplier = 0.85 * volume_mult)
    decision_none = decide_direction(
        btc_4h={"close": 105, "ema_fast": 103, "ema_slow": 100, "ema_slope": 0.1},
        btc_1d={"close": 110, "ema_fast": 105, "ema_slow": 100, "ema_slope": 0.1},
        symbol_1h={"close": 55, "ema_fast": 53, "ema_slow": 50, "ema_slope": 0.1},
        entry_15m={"volume_ratio": 0.8, "quality_score": None},
    )
    # volume_ratio 0.8 is volume ok (1.0). quality unknown is 0.85. 1.0 * 0.85 = 0.85
    assert decision_none.long_allowed is True
    assert decision_none.size_multiplier == 0.85

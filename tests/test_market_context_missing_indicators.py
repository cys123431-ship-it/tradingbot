from utbreakout.market_context import build_market_context


def test_market_context_does_not_invent_favorable_adx_default():
    ctx = build_market_context(values={
        "utbot_direction": "LONG",
        "close": 100.0,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "volume": 1000.0,
    })

    assert ctx.adx == 0.0

    quality = getattr(ctx, "quality", None)
    assert quality is not None

    reasons = list(getattr(quality, "reasons", []) or [])
    assert "MISSING_ADX" in reasons


def test_market_context_preserves_real_adx_value():
    ctx = build_market_context(values={
        "utbot_direction": "LONG",
        "close": 100.0,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "volume": 1000.0,
        "adx": 31.5,
        "plus_di": 35.0,
        "minus_di": 12.0,
        "htf_trend": "UP",
    })

    assert ctx.adx == 31.5

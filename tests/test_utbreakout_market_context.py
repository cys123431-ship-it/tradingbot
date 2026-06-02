from utbreakout.market_context import build_market_context


def _row(close, *, spread_bps=None):
    row = {
        "timestamp": "",
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 1000.0,
    }
    if spread_bps is not None:
        row["spread_bps"] = spread_bps
    return row


def test_market_context_builds_with_missing_derivatives_data():
    rows = [_row(100 + idx) for idx in range(30)]

    context = build_market_context(rows, 25, symbol="BTC/USDT", values={"utbot_direction": "LONG"})

    assert context.symbol == "BTC/USDT"
    assert context.funding_rate is None
    assert context.quality.derivatives_data_available is False
    assert "MISSING_DERIVATIVES_DATA" in context.quality.reasons


def test_market_context_flags_bad_liquidity():
    rows = [_row(100 + idx, spread_bps=25.0) for idx in range(30)]

    context = build_market_context(rows, 25, config={"max_spread_bps": 10.0})

    assert context.quality.bad_liquidity is True
    assert "BAD_LIQUIDITY" in context.quality.reasons

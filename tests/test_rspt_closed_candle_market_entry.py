from utbreakout.relative_strength_pullback import (
    PullbackTrendDecision,
    _closed_rows,
    default_relative_strength_pullback_config,
)


def test_rspt_defaults_to_market_entry_after_closed_candle_confirmation():
    cfg = default_relative_strength_pullback_config()
    assert cfg["entry_execution"] == "market"
    assert cfg["exclude_incomplete_live_candle"] is True
    assert PullbackTrendDecision(symbol="BTC/USDT").entry_execution == "market"


def test_rspt_excludes_the_live_4h_candle_before_signal_evaluation():
    tf_ms = 4 * 60 * 60 * 1000
    rows = [
        {"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"timestamp": tf_ms + 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]
    closed = _closed_rows(rows, "4h", {"exclude_incomplete_live_candle": True}, now_ms=tf_ms + 2)
    assert len(closed) == 1
    assert closed[0]["timestamp"] == 1

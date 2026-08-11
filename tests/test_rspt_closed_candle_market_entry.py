from types import SimpleNamespace

import utbreakout.relative_strength_pullback as relative_strength_pullback_module
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


def test_closed_rows_uses_current_clock_when_caller_omits_now_ms(monkeypatch):
    tf_ms = 60 * 60 * 1000
    current_open_ms = 1_800_000_000_000
    now_ms = current_open_ms + 2
    rows = [
        {
            "timestamp": current_open_ms - tf_ms,
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "volume": 1,
        },
        {
            "timestamp": current_open_ms,
            "open": 2,
            "high": 2,
            "low": 2,
            "close": 2,
            "volume": 2,
        },
        {
            "timestamp": current_open_ms + tf_ms,
            "open": 3,
            "high": 3,
            "low": 3,
            "close": 3,
            "volume": 3,
        },
    ]
    monkeypatch.setattr(
        relative_strength_pullback_module,
        "time",
        SimpleNamespace(time=lambda: now_ms / 1000.0),
    )

    closed = _closed_rows(
        rows,
        "1h",
        {"exclude_incomplete_live_candle": True},
    )

    assert [row["timestamp"] for row in closed] == [current_open_ms - tf_ms]

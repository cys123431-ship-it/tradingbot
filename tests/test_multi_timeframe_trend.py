from utbreakout.multi_timeframe_trend import evaluate_multi_timeframe_trend


def _rows(count, *, start=100.0, step=0.10, breakout=None):
    rows = []
    for index in range(count):
        close = start + index * step
        rows.append({"open": close - 0.1, "high": close + 0.2, "low": close - 0.2, "close": close})
    if breakout == "long":
        rows[-1]["close"] = max(row["high"] for row in rows[-34:-1]) + 1.0
        rows[-1]["high"] = rows[-1]["close"] + 0.2
    elif breakout == "short":
        rows[-1]["close"] = min(row["low"] for row in rows[-34:-1]) - 1.0
        rows[-1]["low"] = rows[-1]["close"] - 0.2
    return rows


def test_single_timeframe_signal_is_live_candidate_at_reduced_risk():
    decision = evaluate_multi_timeframe_trend({
        "15m": _rows(90, breakout="long"),
        "1h": _rows(90),
        "4h": _rows(90),
    })
    assert decision.allowed is True
    assert decision.side == "long"
    assert decision.confirmations == ("15m",)
    assert decision.risk_multiplier == 0.45


def test_multiple_timeframe_confirmations_increase_risk_without_and_requirement():
    decision = evaluate_multi_timeframe_trend({
        "15m": _rows(90, breakout="long"),
        "1h": _rows(90, breakout="long"),
        "4h": _rows(90),
    })
    assert decision.allowed is True
    assert decision.confirmations == ("15m", "1h")
    assert decision.risk_multiplier == 0.75


def test_breakout_against_four_hour_bias_is_rejected():
    decision = evaluate_multi_timeframe_trend({
        "15m": _rows(90, start=200, step=-0.25, breakout="long"),
        "1h": _rows(90, start=200, step=-0.25),
        "4h": _rows(90, start=200, step=-0.25),
    })
    assert decision.allowed is False
    assert decision.side is None
    assert "conflicts" in decision.reason

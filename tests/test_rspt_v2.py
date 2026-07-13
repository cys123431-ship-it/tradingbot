from utbreakout.rspt_v2 import (
    evaluate_pullback_setup,
    residual_strength_percentiles,
    volatility_risk_multiplier,
)


def _rows_from_returns(returns, *, start=100.0, step_ms=14_400_000):
    rows = []
    close = start
    rows.append({
        "timestamp": 0,
        "open": close,
        "high": close * 1.001,
        "low": close * 0.999,
        "close": close,
        "volume": 1000.0,
    })
    for idx, ret in enumerate(returns, start=1):
        previous = close
        close = previous * (1.0 + ret)
        rows.append({
            "timestamp": idx * step_ms,
            "open": previous,
            "high": max(previous, close) * 1.001,
            "low": min(previous, close) * 0.999,
            "close": close,
            "volume": 1000.0 + idx,
        })
    return rows


def test_residual_strength_removes_common_btc_eth_move():
    btc_returns = [0.0010 + (idx % 5 - 2) * 0.0001 for idx in range(130)]
    eth_returns = [0.0008 + (idx % 7 - 3) * 0.00008 for idx in range(130)]

    def candidate(alpha):
        return [
            0.65 * btc + 0.35 * eth + alpha
            for btc, eth in zip(btc_returns, eth_returns)
        ]

    symbols = ["STRONG/USDT:USDT", "MIDDLE/USDT:USDT", "WEAK/USDT:USDT"]
    rows = {
        "BTC/USDT:USDT": _rows_from_returns(btc_returns),
        "ETH/USDT:USDT": _rows_from_returns(eth_returns),
        symbols[0]: _rows_from_returns(candidate(0.0015)),
        symbols[1]: _rows_from_returns(candidate(0.0002)),
        symbols[2]: _rows_from_returns(candidate(-0.0010)),
    }
    result = residual_strength_percentiles(
        symbols,
        rows,
        {
            "relative_strength_reference_symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
            "relative_strength_min_candidates": 3,
            "relative_strength_short_lookback_bars": 28,
            "relative_strength_long_lookback_bars": 100,
            "residual_strength_min_aligned_returns": 40,
            "residual_strength_ridge": 1e-6,
        },
    )

    assert result[symbols[0]]["percentile"] == 1.0
    assert result[symbols[1]]["percentile"] == 0.5
    assert result[symbols[2]]["percentile"] == 0.0
    assert result[symbols[0]]["method"] == "btc_eth_residual_momentum"


def _pullback_rows(include_impulse=True):
    rows = []
    for idx in range(31):
        close = 100.0 + idx * 0.10
        rows.append({
            "timestamp": idx * 14_400_000,
            "open": close - 0.05,
            "high": close + 0.75,
            "low": close - 0.75,
            "close": close,
            "volume": 1000.0,
        })
    if include_impulse:
        rows.append({
            "timestamp": 31 * 14_400_000,
            "open": 103.1,
            "high": 104.8,
            "low": 102.9,
            "close": 104.6,
            "volume": 2000.0,
        })
    else:
        rows.append({
            "timestamp": 31 * 14_400_000,
            "open": 103.1,
            "high": 103.7,
            "low": 102.9,
            "close": 103.4,
            "volume": 1000.0,
        })
    rows.extend([
        {
            "timestamp": 32 * 14_400_000,
            "open": 104.4,
            "high": 104.5,
            "low": 103.6,
            "close": 103.9,
            "volume": 1200.0,
        },
        {
            "timestamp": 33 * 14_400_000,
            "open": 103.9,
            "high": 104.0,
            "low": 103.5,
            "close": 103.7,
            "volume": 1100.0,
        },
        {
            "timestamp": 34 * 14_400_000,
            "open": 103.7,
            "high": 104.4,
            "low": 103.4,
            "close": 104.2,
            "volume": 1300.0,
        },
    ])
    return rows


def test_v2_pullback_requires_impulse_then_reclaim():
    rows = _pullback_rows(include_impulse=True)
    passed, detail = evaluate_pullback_setup(
        "long",
        rows,
        {"signal_ema_pullback": 103.55, "signal_ema_trend": 102.0},
        1.5,
        {
            "donchian_length": 20,
            "atr_length": 14,
            "impulse_lookback_min_bars": 2,
            "impulse_lookback_max_bars": 8,
            "impulse_body_atr_min": 0.55,
            "pullback_tolerance_atr": 0.25,
            "pullback_depth_atr_min": 0.40,
            "pullback_depth_atr_max": 1.20,
            "pullback_body_atr_min": 0.25,
            "pullback_max_wick_ratio": 0.45,
        },
    )

    assert passed is True
    assert detail["prior_impulse_found"] is True
    assert detail["pullback_reclaim"] is True
    assert 0.4 <= detail["pullback_depth_atr"] <= 1.2


def test_v2_pullback_rejects_ema_bounce_without_prior_impulse():
    passed, detail = evaluate_pullback_setup(
        "long",
        _pullback_rows(include_impulse=False),
        {"signal_ema_pullback": 103.55, "signal_ema_trend": 102.0},
        1.5,
        {
            "donchian_length": 20,
            "atr_length": 14,
            "impulse_lookback_min_bars": 2,
            "impulse_lookback_max_bars": 8,
            "impulse_body_atr_min": 0.55,
        },
    )

    assert passed is False
    assert detail["reason"] == "prior_impulse_missing"


def test_volatility_multiplier_only_reduces_risk():
    normal = volatility_risk_multiplier(2.0, 100.0, {})
    high = volatility_risk_multiplier(4.0, 100.0, {})
    extreme = volatility_risk_multiplier(6.0, 100.0, {})

    assert normal[0] == 1.0
    assert high[0] == 0.70
    assert extreme[0] == 0.35

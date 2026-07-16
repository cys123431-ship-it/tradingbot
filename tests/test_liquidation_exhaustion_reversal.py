from utbreakout.liquidation_exhaustion_reversal import (
    LXR_STRATEGY,
    default_liquidation_exhaustion_reversal_config,
    evaluate_liquidation_exhaustion_reversal,
)


def _baseline(count=45, price=100.0):
    rows = []
    current = price
    for index in range(count):
        close = current + (0.03 if index % 2 == 0 else -0.03)
        rows.append({
            "timestamp": index * 900_000,
            "open": current,
            "high": max(current, close) + 0.12,
            "low": min(current, close) - 0.12,
            "close": close,
            "volume": 100.0,
        })
        current = close
    return rows


def _long_setup():
    rows = _baseline()
    start = rows[-1]["close"]
    rows.extend([
        {"timestamp": 45 * 900_000, "open": start, "high": start + 0.10, "low": 98.7, "close": 99.0, "volume": 240.0},
        {"timestamp": 46 * 900_000, "open": 99.0, "high": 99.1, "low": 97.0, "close": 97.3, "volume": 310.0},
        {"timestamp": 47 * 900_000, "open": 97.3, "high": 97.4, "low": 95.5, "close": 96.0, "volume": 350.0},
        {"timestamp": 48 * 900_000, "open": 96.1, "high": 97.7, "low": 96.0, "close": 97.5, "volume": 220.0},
    ])
    return rows


def _short_setup():
    rows = _baseline()
    start = rows[-1]["close"]
    rows.extend([
        {"timestamp": 45 * 900_000, "open": start, "high": 101.3, "low": start - 0.10, "close": 101.0, "volume": 240.0},
        {"timestamp": 46 * 900_000, "open": 101.0, "high": 103.0, "low": 100.9, "close": 102.7, "volume": 310.0},
        {"timestamp": 47 * 900_000, "open": 102.7, "high": 104.5, "low": 102.6, "close": 104.0, "volume": 350.0},
        {"timestamp": 48 * 900_000, "open": 103.9, "high": 104.0, "low": 102.3, "close": 102.5, "volume": 220.0},
    ])
    return rows


def test_defaults_identify_live_lxr_strategy_without_enabling_persisted_runtime():
    cfg = default_liquidation_exhaustion_reversal_config()
    assert LXR_STRATEGY == "liquidation_exhaustion_reversal_v1"
    assert cfg["enabled"] is True
    assert cfg["live_enabled"] is False
    assert cfg["risk_multiplier_cap"] <= 0.45


def test_accepts_confirmed_long_after_down_shock_and_open_interest_drop():
    decision = evaluate_liquidation_exhaustion_reversal(
        _long_setup(),
        {"open_interest_change_1h": -1.20, "open_interest_delta_z": -1.4, "taker_buy_sell_ratio": 1.08},
        {"allowed": True, "state": "bid_support", "direction_support": "long", "risk_multiplier": 0.80},
    )
    assert decision.allowed is True
    assert decision.side == "long"
    assert 0.25 <= decision.risk_multiplier <= 0.45
    assert decision.metrics["structure_stop"] == 95.5
    assert "structure reclaimed" in decision.reason


def test_accepts_confirmed_short_after_up_shock_and_open_interest_drop():
    decision = evaluate_liquidation_exhaustion_reversal(
        _short_setup(),
        {"open_interest_change_1h": -1.10, "open_interest_delta_z": -1.3, "taker_buy_sell_ratio": 0.92},
        {"allowed": True, "state": "ask_pressure", "direction_support": "short", "risk_multiplier": 0.80},
    )
    assert decision.allowed is True
    assert decision.side == "short"
    assert decision.metrics["structure_stop"] == 104.5


def test_missing_open_interest_fails_closed():
    decision = evaluate_liquidation_exhaustion_reversal(
        _long_setup(),
        {"taker_buy_sell_ratio": 1.10},
        {"allowed": True, "state": "bid_support", "direction_support": "long", "risk_multiplier": 1.0},
    )
    assert decision.allowed is False
    assert decision.reason == "open_interest_data_missing"


def test_opposite_l2_pressure_blocks_an_otherwise_valid_reversal():
    decision = evaluate_liquidation_exhaustion_reversal(
        _long_setup(),
        {"open_interest_change_1h": -1.20, "open_interest_delta_z": -1.4, "taker_buy_sell_ratio": 1.08},
        {"allowed": True, "state": "ask_pressure", "direction_support": "short", "risk_multiplier": 0.35},
    )
    assert decision.allowed is False
    assert decision.reason == "l2_direction_conflict"


def test_does_not_enter_on_the_shock_without_a_later_structure_reclaim():
    rows = _long_setup()[:-1]
    decision = evaluate_liquidation_exhaustion_reversal(
        rows,
        {"open_interest_change_1h": -1.20, "open_interest_delta_z": -1.4},
        {"allowed": True, "state": "deep_balanced", "direction_support": None, "risk_multiplier": 0.80},
    )
    assert decision.allowed is False
    assert decision.reason in {"reversal_reclaim_too_weak", "reversal_structure_not_reclaimed", "reversal_body_too_weak"}

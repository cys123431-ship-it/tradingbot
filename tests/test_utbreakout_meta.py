from utbreakout.meta import (
    apply_meta_gate_to_risk,
    build_meta_features,
    decide_meta_trade,
    estimate_meta_probability,
    evaluate_meta_label,
    meta_label_gate,
    probability_to_size_multiplier,
    triple_barrier_label,
    label_from_barrier_outcome,
)


def test_meta_label_marks_take_profit_first_as_positive():
    result = evaluate_meta_label(
        side="long",
        entry_price=100,
        stop_loss=95,
        take_profit=110,
        decision_ts=1000,
        bars=[
            {"timestamp": 2000, "high": 106, "low": 99, "close": 105},
            {"timestamp": 3000, "high": 111, "low": 104, "close": 110},
        ],
        max_bars=3,
    )

    assert result["outcome"] == "tp"
    assert result["label"] == 1


def test_meta_label_marks_stop_loss_first_as_negative():
    result = evaluate_meta_label(
        side="short",
        entry_price=100,
        stop_loss=105,
        take_profit=90,
        decision_ts=1000,
        bars=[{"timestamp": 2000, "high": 106, "low": 94, "close": 103}],
        max_bars=2,
    )

    assert result["outcome"] == "sl"
    assert result["label"] == 0


def test_meta_label_timeout_uses_positive_or_negative_pnl():
    positive = label_from_barrier_outcome({"outcome": "timeout", "pnl_r": 0.15})
    negative = label_from_barrier_outcome({"outcome": "timeout", "pnl_r": -0.01})

    assert positive["label"] == 1
    assert negative["label"] == 0


def test_meta_features_are_safe_when_values_are_missing():
    features = build_meta_features({"adx": None}, side="long", selected_timeframe="15m")

    assert features["adx"] == 0.0
    assert features["side"] == "long"
    assert features["side_long"] == 1.0
    assert features["selected_timeframe"] == "15m"


def test_meta_decision_thresholds():
    assert decide_meta_trade(0.66)["size_multiplier"] == 1.0
    assert decide_meta_trade(0.60)["size_multiplier"] == 0.5
    assert decide_meta_trade(0.52)["action"] == "micro_or_watch"
    assert decide_meta_trade(0.49)["action"] == "block"


def test_meta_probability_improves_with_quality_features():
    strong = estimate_meta_probability({
        "trend_health_score": 82,
        "strategy_quality_score": 80,
        "adaptive_timeframe_score": 74,
        "adx": 32,
        "chop": 38,
        "range_expansion_ratio": 1.5,
        "volume_ratio": 1.4,
        "momentum_12_pct": 1.2,
        "side": "long",
    })
    weak = estimate_meta_probability({
        "trend_health_score": 35,
        "strategy_quality_score": 32,
        "adx": 11,
        "chop": 68,
        "side": "short",
    })

    assert strong > weak
    assert strong >= 0.65
    assert weak < 0.50


def test_triple_barrier_labels_profit_stop_time_correctly():
    rows = [
        {"close": 100, "high": 101, "low": 99, "atr": 5},
        {"close": 103, "high": 110, "low": 102, "atr": 5},
    ]
    stop_rows = [
        {"close": 100, "high": 101, "low": 99, "atr": 5},
        {"close": 98, "high": 99, "low": 94, "atr": 5},
    ]

    assert triple_barrier_label(0, rows, "long", {"label_profit_r": 1.0, "label_max_holding_bars": 2}) == 1
    assert triple_barrier_label(0, stop_rows, "long", {"label_stop_r": 1.0, "label_max_holding_bars": 2}) == -1
    assert triple_barrier_label(0, rows[:1], "long", {"label_max_holding_bars": 2}) == 0


def test_meta_gate_blocks_low_probability_signal_and_allows_high_probability():
    low = meta_label_gate(
        {"side": "short"},
        {"trend_health_score": 20, "strategy_quality_score": 20, "adx": 8, "chop": 80},
        config={"meta_label_gate_enabled": True, "meta_gate_min_prob": 0.55},
    )
    high = meta_label_gate(
        {"side": "long"},
        {"trend_health_score": 85, "strategy_quality_score": 84, "adaptive_timeframe_score": 75, "adx": 35, "chop": 35, "volume_ratio": 1.6},
        config={"meta_label_gate_enabled": True, "meta_gate_min_prob": 0.55},
    )

    assert low.allow is False
    assert high.allow is True
    assert high.size_multiplier > 0


def test_meta_gate_size_multiplier_never_breaks_max_risk():
    gate = meta_label_gate(
        {"side": "long"},
        {"trend_health_score": 90, "strategy_quality_score": 90, "adx": 40, "volume_ratio": 2},
        config={"meta_label_gate_enabled": True},
    )

    assert probability_to_size_multiplier(0.71) == 1.2
    assert apply_meta_gate_to_risk(0.9, gate, {"max_risk_per_trade_pct": 1.0}) <= 1.0

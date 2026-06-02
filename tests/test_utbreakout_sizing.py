from utbreakout.sizing import build_position_risk_multiplier, calculate_adaptive_risk_pct


def test_position_sizing_reduces_risk_in_high_volatility():
    result = build_position_risk_multiplier({
        "atr_pct": 3.0,
        "meta_probability": 0.70,
    })

    assert result["components"]["volatility"] < 0.5
    assert result["risk_multiplier"] < 0.5


def test_position_sizing_reduces_risk_during_drawdown():
    result = build_position_risk_multiplier({
        "atr_pct": 1.0,
        "meta_probability": 0.70,
        "drawdown_pct": 10.0,
    })

    assert result["components"]["drawdown"] < 1.0
    assert result["risk_multiplier"] < 1.0


def test_position_sizing_blocks_after_hard_loss_streak():
    result = build_position_risk_multiplier({
        "meta_probability": 0.70,
        "consecutive_losses": 5,
    })

    assert result["blocked"] is True
    assert result["risk_multiplier"] == 0.0


def test_position_sizing_keeps_good_signal_near_full_risk():
    result = build_position_risk_multiplier({
        "atr_pct": 0.8,
        "meta_probability": 0.72,
        "trend_health_multiplier": 1.0,
        "strategy_quality_multiplier": 1.0,
        "regime_risk_multiplier": 1.0,
        "drawdown_pct": 0,
        "consecutive_losses": 0,
    })

    assert result["blocked"] is False
    assert result["risk_multiplier"] == 1.0


def test_adaptive_risk_reduces_and_caps_multiplier_stack():
    risk = calculate_adaptive_risk_pct(
        {"side": "long"},
        {"atr_percentile": 90, "account": {"consecutive_losses": 2}, "portfolio": {}},
        {"risk_multiplier": 1.0},
        {"size_multiplier": 1.0},
        {"size_multiplier": 1.2},
        {"risk_per_trade_pct": 0.8, "max_risk_per_trade_pct": 1.0},
    )

    assert 0 < risk <= 1.0
    assert risk < 0.8


def test_adaptive_risk_blocks_after_three_losses_daily_loss_and_exposure():
    cfg = {"risk_per_trade_pct": 0.5}

    assert calculate_adaptive_risk_pct(context={"account": {"consecutive_losses": 3}}, config=cfg) == 0.0
    assert calculate_adaptive_risk_pct(context={"account": {"daily_loss_r": -2.0}}, config=cfg) == 0.0
    assert calculate_adaptive_risk_pct(context={"portfolio": {"total_open_risk_pct": 2.0}}, config=cfg) == 0.0
    assert calculate_adaptive_risk_pct(context={"portfolio": {"same_direction_positions": 2}}, config=cfg) == 0.0

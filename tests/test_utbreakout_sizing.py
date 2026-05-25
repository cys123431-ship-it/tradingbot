from utbreakout.sizing import build_position_risk_multiplier


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

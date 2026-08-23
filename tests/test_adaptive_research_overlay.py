from utbreakout.adaptive_research_overlay import (
    evaluate_adaptive_research_overlay,
)


def _stable_metrics(**overrides):
    metrics = {
        "score": 78.0,
        "horizon_scores": {24: 1.2, 72: 1.5, 168: 1.7},
        "weighted_momentum": 0.72,
        "fast_momentum_retention": 0.70,
        "trend_clarity": 0.72,
        "trend_efficiency": 0.55,
        "short_volatility": 0.010,
        "long_volatility": 0.009,
        "signed_fast_ema_distance_atr": 0.35,
        "ema_crossover": False,
        "compression_breakout": False,
        "pullback_resumption": False,
        "impulse_breakout": False,
    }
    metrics.update(overrides)
    return metrics


def test_stable_regime_neutral_carry_keeps_candidate_and_never_upsizes():
    result = evaluate_adaptive_research_overlay(
        "long",
        _stable_metrics(),
        {"funding_rate": 0.0001, "basis_pct": 0.05},
        base_score=78.0,
    )
    assert result["allowed"] is True
    assert 0.0 < result["risk_multiplier"] <= 1.0
    assert result["regime_continuity"] > 0.58
    assert result["carry_severity"] == 0.0


def test_mature_continuation_with_fast_sleeve_reversal_is_rejected():
    result = evaluate_adaptive_research_overlay(
        "long",
        _stable_metrics(
            horizon_scores={24: -0.8, 72: 0.25, 168: 1.4},
            weighted_momentum=0.20,
            fast_momentum_retention=0.0,
            trend_clarity=0.30,
            trend_efficiency=0.20,
        ),
        {},
        base_score=70.0,
    )
    assert result["allowed"] is False
    assert result["transition_risk"] is True
    assert result["code"] in {
        "REJECTED_RESEARCH_OVERLAY_REGIME_PERSISTENCE",
        "REJECTED_RESEARCH_OVERLAY_SCORE",
    }


def test_fresh_breakout_survives_volatility_shock_but_is_materially_de_risked():
    result = evaluate_adaptive_research_overlay(
        "long",
        _stable_metrics(
            compression_breakout=True,
            short_volatility=0.035,
            long_volatility=0.012,
            score=90.0,
        ),
        {},
        base_score=90.0,
    )
    assert result["allowed"] is True
    assert result["volatility_shock_ratio"] > 2.75
    assert result["risk_multiplier"] < 0.60
    assert result["risk_multiplier"] <= 1.0


def test_adverse_perpetual_carry_reduces_score_and_risk():
    result = evaluate_adaptive_research_overlay(
        "long",
        _stable_metrics(score=85.0),
        {"funding_rate": 0.0008, "basis_pct": 0.28},
        base_score=85.0,
    )
    assert result["allowed"] is True
    assert 0.0 < result["carry_severity"] < 1.0
    assert result["carry_risk_multiplier"] < 1.0
    assert result["adjusted_score"] < 85.0 + result["regime_score_adjustment"]
    assert result["risk_multiplier"] < 1.0


def test_extreme_crowded_extended_mature_entry_is_rejected():
    result = evaluate_adaptive_research_overlay(
        "long",
        _stable_metrics(
            score=95.0,
            signed_fast_ema_distance_atr=1.2,
        ),
        {"funding_rate": 0.0013, "basis_pct": 0.45},
        base_score=95.0,
    )
    assert result["allowed"] is False
    assert result["code"] == "REJECTED_RESEARCH_OVERLAY_CROWDED_EXTENSION"


def test_missing_derivatives_context_is_neutral_not_failure():
    result = evaluate_adaptive_research_overlay(
        "long",
        _stable_metrics(),
        {},
        base_score=78.0,
    )
    assert result["allowed"] is True
    assert result["carry_severity"] == 0.0
    assert result["carry_risk_multiplier"] == 1.0


def test_short_side_uses_direction_signed_carry_symmetrically():
    long_adverse = evaluate_adaptive_research_overlay(
        "long",
        _stable_metrics(),
        {"funding_rate": 0.0008, "basis_pct": 0.25},
        base_score=85.0,
    )
    short_metrics = _stable_metrics(
        horizon_scores={24: -1.2, 72: -1.5, 168: -1.7},
        weighted_momentum=-0.72,
    )
    short_adverse = evaluate_adaptive_research_overlay(
        "short",
        short_metrics,
        {"funding_rate": -0.0008, "basis_pct": -0.25},
        base_score=85.0,
    )
    assert abs(long_adverse["carry_severity"] - short_adverse["carry_severity"]) < 1e-12
    assert abs(long_adverse["risk_multiplier"] - short_adverse["risk_multiplier"]) < 1e-12


def test_favorable_carry_never_increases_position_multiplier_above_one():
    result = evaluate_adaptive_research_overlay(
        "long",
        _stable_metrics(
            short_volatility=0.004,
            long_volatility=0.005,
        ),
        {"funding_rate": -0.0010, "basis_pct": -0.30},
        base_score=82.0,
    )
    assert result["allowed"] is True
    assert result["favorable_carry_score_bonus"] > 0
    assert result["risk_multiplier"] <= 1.0

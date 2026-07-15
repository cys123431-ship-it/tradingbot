import pytest

from utbreakout.alpha_engine import evaluate_profit_alpha
from utbreakout.intelligence import (
    build_signal_attribution,
    evaluate_data_quality_engine,
    evaluate_execution_quality_engine,
    evaluate_market_regime_engine,
    evaluate_overfit_governance,
    evaluate_protection_health_engine,
    run_overfit_backtest,
    run_strategy_replay,
)


def _values(side="long"):
    return {
        "close": 100.0,
        "adx": 30.0,
        "chop": 38.0,
        "plus_di": 28.0 if side == "long" else 12.0,
        "minus_di": 12.0 if side == "long" else 28.0,
        "volume_ratio": 1.45,
        "range_expansion": 1.20,
        "coin_selector_score": 80.0,
        "trend_health_score": 82.0,
        "strategy_quality_score": 78.0,
        "quality_score_v2_score": 80.0,
        "taker_buy_sell_ratio": 1.08 if side == "long" else 0.92,
        "rolling_ofi": 12.0 if side == "long" else -12.0,
        "orderbook_imbalance_pct": 10.0 if side == "long" else -10.0,
        "open_interest_change_1h": 0.35,
        "funding_rate": -0.0001 if side == "long" else 0.0001,
        "basis_pct": 0.01,
        "long_short_ratio": 1.1,
        "futures_spread_pct": 0.03,
        "bid_depth_usdt": 100000.0,
        "ask_depth_usdt": 110000.0,
        "market_regime_context": {
            "items": {
                "BTC/USDT": {"direction": side, "return_lookback_pct": 0.7},
                "ETH/USDT": {"direction": side, "return_lookback_pct": 0.5},
            }
        },
    }


def test_market_regime_engine_scores_aligned_trend_without_filtering_scanner():
    decision = evaluate_market_regime_engine(side="short", values=_values("short"))

    assert decision.allowed is True
    assert decision.regime in {"STRONG_BEAR_TREND", "BEAR_TREND"}
    assert decision.risk_multiplier > 0


def test_data_and_execution_quality_block_only_bad_execution_inputs():
    stale = evaluate_data_quality_engine(
        values={"close": 100.0, "now_ts": 2_000_000.0, "feed_last_ts": 1_999_000.0},
        config={"data_quality_max_feed_age_sec": 240.0},
    )
    wide = evaluate_execution_quality_engine(
        values={"close": 100.0, "futures_spread_pct": 0.30},
        config={"execution_quality_max_spread_pct": 0.12},
    )

    assert stale.allowed is False
    assert any("stale market feed" in item for item in stale.blockers)
    assert wide.allowed is False
    assert any("spread" in item for item in wide.blockers)


def test_protection_health_execution_gate_requires_sl_and_tp_when_enabled():
    decision = evaluate_protection_health_engine(
        values={"entry_price": 100.0, "stop_loss": 98.0},
        config={"protection_health_require_plan_fields": True},
    )

    assert decision.allowed is False
    assert any("take-profit" in item for item in decision.blockers)


def test_overfit_governance_blocks_negative_oos_after_enough_samples():
    decision = evaluate_overfit_governance(
        meta_stats={
            "short:STRONG_DOWNTREND_SHORT:BREAKOUT:TREND_RUNNER:BEAR_TREND": {
                "sample_count": 24,
                "expectancy_r": 0.08,
                "oos_expectancy_r": -0.12,
                "profit_factor": 1.05,
            }
        },
        side="short",
        engine="STRONG_DOWNTREND_SHORT",
        entry_type="BREAKOUT",
        exit_policy="TREND_RUNNER",
        regime="BEAR_TREND",
    )

    assert decision.allowed is False
    assert any("OOS expectancy" in item for item in decision.blockers)


def test_overfit_governance_reduces_warmup_risk_and_requires_positive_realized_edge():
    warmup = evaluate_overfit_governance(
        meta_stats={},
        side="long",
        engine="TREND_CONTINUATION",
        entry_type="BREAKOUT",
        exit_policy="TREND_RUNNER",
        regime="BULL_TREND",
    )
    failed = evaluate_overfit_governance(
        meta_stats={
            "long:TREND_CONTINUATION": {
                "sample_count": 20,
                "expectancy_r": -0.001,
            }
        },
        side="long",
        engine="TREND_CONTINUATION",
        entry_type="BREAKOUT",
        exit_policy="TREND_RUNNER",
        regime="BULL_TREND",
    )

    assert warmup.allowed is True
    assert warmup.risk_multiplier == pytest.approx(0.70)
    assert failed.allowed is False
    assert any("overfit expectancy" in item for item in failed.blockers)


def test_overfit_backtest_reports_multiple_testing_failure():
    trades = [{"pnl_r": 1.0, "pnl_usdt": 1.0}] * 20 + [{"pnl_r": -1.0, "pnl_usdt": -1.0}] * 20

    report = run_overfit_backtest(
        trades,
        {
            "overfit_min_samples": 12,
            "overfit_walk_forward_train_size": 10,
            "overfit_walk_forward_test_size": 10,
            "overfit_max_pbo": 0.30,
        },
        number_of_trials=80,
    )

    assert report.passed is False
    assert report.pbo >= 0.5
    assert report.adjusted_expectancy_r <= 0


def test_strategy_replay_uses_shadow_barrier_without_changing_scanner_selection():
    candidates = [{
        "symbol": "DOGE/USDT:USDT",
        "side": "short",
        "entry_price": 100.0,
        "stop_loss": 102.0,
        "take_profit": 96.0,
        "decision_ts": 1,
    }]
    bars = {
        "DOGE/USDT:USDT": [
            {"timestamp": 2, "open": 100.0, "high": 100.5, "low": 95.5, "close": 96.0},
        ]
    }

    result = run_strategy_replay(candidates, bars)

    assert len(result) == 1
    assert result[0]["symbol"] == "DOGE/USDT:USDT"
    assert result[0]["replay_outcome"] == "tp"


def test_profit_alpha_contains_new_engine_components_without_blocking_good_signal():
    decision = evaluate_profit_alpha(side="long", values=_values("long"))

    assert decision.allowed is True
    assert decision.components["market_regime_engine_score"] > 0
    assert decision.components["data_quality_score"] > 0
    assert decision.components["execution_quality_score"] > 0
    assert decision.components["overfit_governance_score"] > 0
    assert any("attribution" in item for item in decision.reasons)


def test_signal_attribution_key_is_context_not_scanner_filter():
    attribution = build_signal_attribution(
        side="long",
        engine="TREND_CONTINUATION",
        entry_type="BREAKOUT",
        exit_policy="TREND_RUNNER",
        market_regime="BULL_TREND",
        values={"entry_timeframe": "15m"},
    )

    assert attribution.tags["side"] == "long"
    assert "TREND_CONTINUATION" in attribution.key

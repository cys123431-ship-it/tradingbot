from utbreakout.small_account_regime import (
    evaluate_small_account_exhaustion_reversal,
    resolve_regime_ensemble_candidate,
    reversal_exit_plan_overrides,
)
from bot_runtime.signal_entry import build_durable_entry_plan_summary


def _long_sweep_rows():
    rows = []
    for index in range(40):
        center = 100.0 + (index % 3 - 1) * 0.01
        rows.append({
            "open": center,
            "high": center + 0.20,
            "low": center - 0.20,
            "close": center + 0.02,
            "volume": 100.0,
        })
    rows.append({
        "open": 99.90,
        "high": 100.30,
        "low": 99.45,
        "close": 100.15,
        "volume": 180.0,
    })
    return rows


def test_exhaustion_reversal_accepts_confirmed_sweep_in_weak_regime():
    result = evaluate_small_account_exhaustion_reversal(
        _long_sweep_rows(),
        trend_metrics={
            "weighted_momentum": -0.05,
            "trend_clarity": 0.12,
            "horizon_votes": {24: "short", 72: "long", 168: "flat"},
        },
        futures_context={
            "rolling_orderbook_imbalance_pct": 4.0,
            "taker_buy_sell_ratio": 1.08,
            "orderflow_age_seconds": 20.0,
        },
        market_regime_context={
            "items": {
                "BTC/USDT": {"direction": "unknown"},
                "ETH/USDT": {"direction": "unknown"},
            }
        },
    )

    assert result["allowed"] is True
    assert result["side"] == "long"
    assert result["source"] == "exhaustion_reversal"
    assert result["risk_tier"] == "base"
    assert result["reversal_tp2_r"] > result["reversal_tp1_r"]


def test_exhaustion_reversal_blocks_strong_broad_countertrend():
    result = evaluate_small_account_exhaustion_reversal(
        _long_sweep_rows(),
        trend_metrics={
            "weighted_momentum": -0.72,
            "trend_clarity": 0.82,
            "horizon_votes": {24: "short", 72: "short", 168: "short"},
        },
        futures_context={
            "rolling_orderbook_imbalance_pct": 5.0,
            "orderflow_age_seconds": 10.0,
        },
        market_regime_context={
            "items": {
                "BTC/USDT": {"direction": "short"},
                "ETH/USDT": {"direction": "short"},
            }
        },
    )

    assert result["allowed"] is False
    assert result["code"] == "REGIME_REVERSAL_BLOCKED_OR_UNCONFIRMED"


def test_regime_router_never_overrides_valid_primary_candidate():
    primary = {
        "allowed": True,
        "side": "short",
        "score": 73.0,
        "source": "trend_only",
    }
    reversal = {
        "allowed": True,
        "side": "long",
        "score": 88.0,
        "source": "exhaustion_reversal",
    }

    resolved = resolve_regime_ensemble_candidate(primary, reversal)

    assert resolved["allowed"] is True
    assert resolved["side"] == "short"
    assert resolved["source"] == "trend_only"
    assert resolved["regime_engine"] == "trend_continuation"


def test_regime_router_uses_reversal_only_when_primary_is_waiting():
    resolved = resolve_regime_ensemble_candidate(
        {"allowed": False, "reason": "no trend/event"},
        {
            "allowed": True,
            "side": "long",
            "score": 76.0,
            "source": "exhaustion_reversal",
            "reason": "confirmed sweep",
            "code": "SMALL_ACCOUNT_EXHAUSTION_REVERSAL_LONG",
        },
    )

    assert resolved["allowed"] is True
    assert resolved["side"] == "long"
    assert resolved["source"] == "exhaustion_reversal"


def test_reversal_exit_profile_is_finite_and_does_not_change_trend_exits():
    assert reversal_exit_plan_overrides("trend_only", {}) == {}

    overrides = reversal_exit_plan_overrides(
        "exhaustion_reversal",
        {"reversal_tp1_r": 0.75, "reversal_tp2_r": 1.40},
    )

    assert overrides["partial_take_profit_enabled"] is True
    assert overrides["second_take_profit_enabled"] is True
    assert (
        overrides["partial_take_profit_ratio"]
        + overrides["second_take_profit_ratio"]
    ) == 1.0
    assert overrides["runner_pct"] == 0.0
    assert overrides["preserve_runner_qty"] is False
    assert overrides["adaptive_trend_pyramid_enabled"] is False
    assert overrides["convex_rotation_exit_enabled"] is False
    assert overrides["take_profit_front_run_atr"] == 0.0
    assert overrides["soft_stop_enabled"] is False


def test_reversal_profile_survives_durable_entry_plan_summary():
    summary = build_durable_entry_plan_summary({
        "strategy": "adaptive_breakout_trend_v1",
        "adaptive_regime_engine": "exhaustion_reversal",
        "adaptive_regime_profile": "small_account_regime_ensemble_v1",
        "reversal_mean_target_price": 101.25,
        "partial_take_profit_r_multiple": 0.75,
        "second_take_profit_r_multiple": 1.40,
    })

    assert summary["adaptive_regime_engine"] == "exhaustion_reversal"
    assert summary["adaptive_regime_profile"] == "small_account_regime_ensemble_v1"
    assert summary["reversal_mean_target_price"] == 101.25

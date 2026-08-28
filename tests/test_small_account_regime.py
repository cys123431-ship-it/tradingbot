from utbreakout.small_account_regime import (
    SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
    evaluate_multi_timeframe_regime,
    evaluate_regime_challenger_promotion,
    evaluate_small_account_exhaustion_reversal,
    resolve_regime_ensemble_candidate,
    resolve_small_account_evidence_allocation,
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


def test_regime_router_keeps_unvalidated_reversal_shadow_only():
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

    assert resolved["allowed"] is False
    assert resolved["code"] == "REGIME_CHALLENGER_SHADOW_ONLY"
    assert resolved["reversal_shadow_only"] is True


def test_regime_router_promotes_reversal_only_after_validation():
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
        promotion_status={"qualified": True, "live_allowed": True},
    )

    assert resolved["allowed"] is True
    assert resolved["side"] == "long"
    assert resolved["source"] == "exhaustion_reversal"


def test_regime_router_waits_when_trend_timeframes_are_ambiguous():
    resolved = resolve_regime_ensemble_candidate(
        {
            "allowed": True,
            "side": "long",
            "score": 74.0,
            "source": "trend_only",
            "fresh_continuation": True,
        },
        {"allowed": False},
        multi_timeframe_context={
            "available": True,
            "ambiguous": True,
            "weighted_direction_score": 0.02,
        },
    )

    assert resolved["allowed"] is False
    assert resolved["code"] == "REGIME_ROUTER_TIMEFRAME_AMBIGUOUS"


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
        "adaptive_regime_profile": SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
        "small_account_regime_transition": "persistent_up",
        "small_account_multi_speed_agreement": 0.91,
        "small_account_regime_persistence_score": 0.82,
        "reversal_mean_target_price": 101.25,
        "partial_take_profit_r_multiple": 0.75,
        "second_take_profit_r_multiple": 1.40,
    })

    assert summary["adaptive_regime_engine"] == "exhaustion_reversal"
    assert summary["adaptive_regime_profile"] == SMALL_ACCOUNT_REGIME_PROFILE_VERSION
    assert summary["small_account_regime_transition"] == "persistent_up"
    assert summary["small_account_multi_speed_agreement"] == 0.91
    assert summary["reversal_mean_target_price"] == 101.25


def _trend_rows(direction=1.0):
    rows = []
    for index in range(90):
        close = 100.0 + direction * index * 0.25
        rows.append({
            "open": close - direction * 0.05,
            "high": close + 0.20,
            "low": close - 0.20,
            "close": close,
            "volume": 100.0,
        })
    return rows


def test_multi_timeframe_regime_is_weighted_not_all_timeframe_and():
    context = evaluate_multi_timeframe_regime({
        "15m": _trend_rows(-1.0),
        "1h": _trend_rows(1.0),
        "4h": _trend_rows(1.0),
        "1d": _trend_rows(1.0),
    })

    assert context["available"] is True
    assert context["direction"] == "long"
    assert context["regime"] == "up"
    assert context["disagreements"] == 1
    assert 0.0 <= context["multi_speed_agreement"] <= 1.0
    assert set(context["snapshots"]["1h"]["speed_scores"]) == {
        "fast",
        "medium",
        "slow",
    }


def _router_context(*, persistent=True):
    return {
        "available": True,
        "ambiguous": False,
        "weighted_direction_score": 0.45,
        "direction": "long",
        "multi_speed_agreement": 0.92 if persistent else 0.52,
        "persistence_score": 0.88 if persistent else 0.10,
        "transition": "persistent_up" if persistent else "transition_or_mixed",
        "mature": False,
    }


def _primary_long_candidate():
    return {
        "allowed": True,
        "side": "long",
        "score": 72.0,
        "source": "trend_only",
        "fresh_continuation": True,
    }


def test_regime_router_rewards_persistent_multi_speed_trend_without_an_and_gate():
    persistent = resolve_regime_ensemble_candidate(
        _primary_long_candidate(),
        {"allowed": False},
        multi_timeframe_context=_router_context(persistent=True),
    )
    transitional = resolve_regime_ensemble_candidate(
        _primary_long_candidate(),
        {"allowed": False},
        multi_timeframe_context=_router_context(persistent=False),
    )

    assert persistent["allowed"] is True
    assert transitional["allowed"] is True
    assert (
        persistent["selected_net_edge"]["win_probability"]
        > transitional["selected_net_edge"]["win_probability"]
    )
    assert persistent["regime_profile"] == SMALL_ACCOUNT_REGIME_PROFILE_VERSION


def test_regime_router_uses_only_fresh_orderflow_and_direction_aware_basis():
    supportive = resolve_regime_ensemble_candidate(
        _primary_long_candidate(),
        {"allowed": False},
        multi_timeframe_context=_router_context(),
        cost_context={
            "basis_pct": -0.30,
            "orderflow_age_seconds": 30.0,
            "rolling_orderbook_imbalance_pct": 12.0,
            "taker_buy_sell_ratio": 1.12,
        },
    )
    stale_adverse = resolve_regime_ensemble_candidate(
        _primary_long_candidate(),
        {"allowed": False},
        multi_timeframe_context=_router_context(),
        cost_context={
            "basis_pct": 0.30,
            "orderflow_age_seconds": 180.0,
            "rolling_orderbook_imbalance_pct": 12.0,
            "taker_buy_sell_ratio": 1.12,
        },
    )

    assert (
        supportive["selected_net_edge"]["win_probability"]
        > stale_adverse["selected_net_edge"]["win_probability"]
    )
    assert (
        stale_adverse["selected_net_edge"]["probability_adjustments"][
            "fresh_orderflow"
        ]
        == 0.0
    )


def _momentum_allocation_candidate(*, side="long", source="trend_only"):
    return {
        "allowed": True,
        "side": side,
        "source": source,
        "score": 88.0,
        "fresh_continuation": True,
        "regime_engine": "trend_continuation",
        "selected_net_edge": {
            "orderflow_age_seconds": 20.0,
            "probability_adjustments": {
                "fresh_orderflow": 0.012,
                "basis": 0.004,
            },
        },
    }


def _momentum_selector():
    return {
        "convex_rotation_score": 88.0,
        "convex_rotation_percentile": 94.0,
        "convex_rotation_universe_size": 40,
    }


def test_evidence_allocation_expands_only_corroborated_persistent_crypto_long():
    allocation = resolve_small_account_evidence_allocation(
        _momentum_allocation_candidate(),
        _router_context(persistent=True),
        _momentum_selector(),
        risk_tier="elite",
        initial_margin_fraction=0.65,
    )

    assert allocation["applied"] is True
    assert allocation["evidence_tier"] == "elite"
    assert allocation["margin_multiplier"] == 1.25
    assert allocation["initial_margin_fraction"] == 0.8125

    summary = build_durable_entry_plan_summary({
        "small_account_evidence_allocation_profile": allocation["profile"],
        "small_account_evidence_allocation_applied": allocation["applied"],
        "small_account_evidence_allocation_tier": allocation["evidence_tier"],
        "small_account_evidence_margin_multiplier": allocation["margin_multiplier"],
        "small_account_evidence_base_margin_fraction": allocation[
            "base_initial_margin_fraction"
        ],
        "small_account_evidence_reason": allocation["reason"],
        "small_account_evidence": allocation["evidence"],
    })
    assert summary["small_account_evidence_allocation_applied"] is True
    assert summary["small_account_evidence_allocation_tier"] == "elite"
    assert summary["small_account_evidence_margin_multiplier"] == 1.25


def test_evidence_allocation_never_reduces_short_event_tradfi_or_stale_flow():
    cases = (
        (_momentum_allocation_candidate(side="short"), False),
        (_momentum_allocation_candidate(source="event_only"), False),
        (_momentum_allocation_candidate(), True),
    )
    for candidate, tradfi in cases:
        allocation = resolve_small_account_evidence_allocation(
            candidate,
            _router_context(persistent=True),
            _momentum_selector(),
            risk_tier="elite",
            initial_margin_fraction=0.65,
            tradfi=tradfi,
        )
        assert allocation["applied"] is False
        assert allocation["initial_margin_fraction"] == 0.65

    stale = _momentum_allocation_candidate()
    stale["selected_net_edge"]["orderflow_age_seconds"] = 180.0
    stale["selected_net_edge"]["probability_adjustments"]["fresh_orderflow"] = 0.0
    allocation = resolve_small_account_evidence_allocation(
        stale,
        _router_context(persistent=True),
        _momentum_selector(),
        risk_tier="elite",
        initial_margin_fraction=0.65,
    )
    assert allocation["applied"] is False
    assert allocation["initial_margin_fraction"] == 0.65


def test_challenger_promotion_requires_independent_regime_diversified_net_results():
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    regimes = ("up", "down", "range")
    events = []
    for index in range(120):
        events.append({
            "event": "shadow_outcome",
            "shadow_engine": "exhaustion_reversal",
            "shadow_key": f"candidate-{index}",
            "pnl_r": 1.50 if index % 3 else -0.30,
            "estimated_cost_r": 0.05,
            "symbol": f"COIN{index % 8}/USDT",
            "shadow_regime": regimes[index % 3],
            "ts": (start + timedelta(hours=index)).isoformat(),
        })

    status = evaluate_regime_challenger_promotion(events)

    assert status["qualified"] is True
    assert status["live_allowed"] is True
    assert status["sample_count"] == 120
    assert status["deflated_sharpe_pass"] is True
    assert status["pbo"] <= 0.45


def test_challenger_promotion_rejects_symbol_concentration_and_missing_regimes():
    events = [
        {
            "event": "shadow_outcome",
            "shadow_engine": "exhaustion_reversal",
            "shadow_key": f"candidate-{index}",
            "pnl_r": 1.0,
            "estimated_cost_r": 0.05,
            "symbol": "ONLY/USDT",
            "shadow_regime": "up",
        }
        for index in range(100)
    ]

    status = evaluate_regime_challenger_promotion(events)

    assert status["qualified"] is False
    assert any("range regime samples" in reason for reason in status["reasons"])
    assert any("symbol concentration" in reason for reason in status["reasons"])

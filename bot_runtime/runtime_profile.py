"""UTBreakout runtime profile, stable Set registry, and effective configuration."""

from __future__ import annotations

from utbreakout.ev_adaptive import EV_ADAPTIVE_PROFILE_VERSION

BINANCE_FAPI_PUBLIC_BASE_URL = 'https://fapi.binance.com'
UTBREAKOUT_ACTIVE_SET_MAX = 64
UTBREAKOUT_DEFAULT_SET_ID = 2
UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID = 64
UTBREAKOUT_AUTO_TIMEFRAMES = ["15m", "30m", "1h"]
UTBREAKOUT_RUNTIME_PROFILE = EV_ADAPTIVE_PROFILE_VERSION
UTBREAKOUT_EFFECTIVE_PROFILE_VERSION = EV_ADAPTIVE_PROFILE_VERSION

# Live AUTO set hardening:
# Keep live AUTO narrow to reduce many-set overfitting / multiple-testing risk.
UTBREAKOUT_LIVE_AUTO_SET_WHITELIST = frozenset({64})

# Selected Set identity filters that must hard-block when they fail in live mode.
# These are the core filters that define the selected Set itself.
UTBREAKOUT_CORE_SET_FILTER_HARD_NAMES = frozenset({
    'Donchian 돌파',
    'BB 밴드 돌파',
    'Keltner 돌파',
    'Range 확장봉',
    '거래량 급증',
    '상대 거래량',
    'Rolling OFI 확인',
    'OI/Funding Squeeze',
    'Squeeze Release 돌파',
    'Futures 수급 불균형',
    'OI/Funding 과열회피',
})

UTBREAKOUT_CORE_SET_FILTER_HARD_CODES = frozenset({
    'REJECTED_DONCHIAN_NO_BREAKOUT',
    'REJECTED_RELATIVE_VOLUME_CORE',
    'REJECTED_ROLLING_OFI',
    'REJECTED_OI_FUNDING_SQUEEZE',
    'REJECTED_SQUEEZE_RELEASE',
    'REJECTED_PREDICTION_FLOW',
    'REJECTED_PREDICTION_CROWDING',
})


def _safe_set_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value or "")
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else None


def _estimate_set_complexity_penalty(set_info):
    """Penalize over-specific Sets without turning complexity into a hard block."""
    if not isinstance(set_info, dict):
        return 0.0

    name = str(
        set_info.get("name")
        or set_info.get("label")
        or set_info.get("description")
        or ""
    ).lower()
    penalty = 0.0
    for keyword in (
        "rolling ofi",
        "orderflow",
        "imbalance",
        "aroon",
        "mtf",
        "volatility score",
        "confirmation",
        "squeeze",
        "pullback",
    ):
        if keyword in name:
            penalty += 0.7

    condition_count = 0
    for key in ("filters", "conditions", "confirmations", "requirements"):
        value = set_info.get(key)
        if isinstance(value, (list, tuple, set)):
            condition_count += len(value)
        elif isinstance(value, dict):
            condition_count += len(value)
    if condition_count >= 4:
        penalty += 1.0
    elif condition_count >= 2:
        penalty += 0.5
    return min(penalty, 3.0)


def _rank_utbreakout_sets_stably(candidates, previous_set_id=None, cfg=None):
    """Rank Set candidates with hysteresis and a bounded complexity tie-break."""
    cfg = cfg or {}
    switch_margin = float(cfg.get("set_selection_switch_margin", 3.0) or 3.0)
    complexity_penalty_enabled = bool(cfg.get("set_complexity_penalty_enabled", True))
    previous_set_id = _safe_set_id(previous_set_id)

    normalized = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        raw_score = item.get("score")
        if raw_score is None:
            raw_score = item.get("set_score")
        if raw_score is None:
            raw_score = item.get("total_score")
        try:
            raw_score = float(raw_score)
        except (TypeError, ValueError):
            continue

        set_id = _safe_set_id(item.get("set_id") or item.get("id") or item.get("set"))
        complexity_penalty = (
            _estimate_set_complexity_penalty(item)
            if complexity_penalty_enabled
            else 0.0
        )
        adjusted_score = raw_score - complexity_penalty
        if previous_set_id is not None and set_id == previous_set_id:
            adjusted_score += switch_margin

        normalized.append({
            "item": item,
            "set_id": set_id,
            "raw_score": raw_score,
            "adjusted_score": adjusted_score,
            "complexity_penalty": complexity_penalty,
        })

    normalized.sort(key=lambda row: row["adjusted_score"], reverse=True)
    return normalized


def _first_present_metric(metrics, names, default=None):
    metrics = metrics if isinstance(metrics, dict) else {}
    for name in names:
        if name in metrics and metrics.get(name) is not None:
            return metrics.get(name)
    return default


def _classify_set_filter_result(side, passed, detail, metrics=None, cfg=None):
    """Classify selected-Set confirmation failures as pass, reduce, or block."""
    metrics = metrics or {}
    cfg = cfg or {}
    if passed:
        return True, 1.0, detail

    try:
        spread = float(_first_present_metric(
            metrics,
            ("futures_spread_pct", "spread_pct", "spread"),
        ))
    except (TypeError, ValueError):
        spread = None
    spread_limit = float(
        cfg.get(
            "rolling_ofi_spread_max_pct",
            cfg.get("set32_max_spread_pct", 0.05),
        )
        or 0.05
    )
    if spread is not None and spread > spread_limit:
        return False, 0.0, (
            f"{detail}; spread {spread:.4f}%>{spread_limit:.4f}%"
        )

    try:
        imbalance = float(_first_present_metric(
            metrics,
            (
                "imbalance",
                "imb",
                "orderflow_imbalance",
                "rolling_orderbook_imbalance_pct",
                "orderbook_imbalance_pct",
            ),
            0.0,
        ))
    except (TypeError, ValueError):
        imbalance = 0.0
    try:
        taker = float(_first_present_metric(
            metrics,
            ("taker", "taker_ratio", "taker_buy_sell_ratio"),
            1.0,
        ))
    except (TypeError, ValueError):
        taker = 1.0
    try:
        samples = int(float(_first_present_metric(
            metrics,
            ("samples", "rolling_ofi_samples"),
            0,
        )))
    except (TypeError, ValueError):
        samples = 0

    if samples < 3:
        return "reduced", 0.75, f"{detail}; soft fail due to low samples {samples}<3"
    if samples < 5:
        return "reduced", 0.65, f"{detail}; soft fail due to limited samples {samples}<5"

    side_l = str(side or "").lower()
    if side_l == "long" and imbalance <= -8.0 and taker <= 0.98:
        return "reduced", 0.35, f"{detail}; hard opposite orderflow reduced for LONG"
    if side_l == "short" and imbalance >= 8.0 and taker >= 1.02:
        return "reduced", 0.35, f"{detail}; hard opposite orderflow reduced for SHORT"
    return "reduced", 0.50, f"{detail}; set confirmation soft fail"


def _apply_utbreakout_risk_multiplier_floor(value, cfg=None):
    """Keep accepted soft-reduced entries meaningful without bypassing hard blocks."""
    cfg = cfg or {}
    try:
        multiplier = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        multiplier = 1.0
    if multiplier <= 0.0:
        return 0.0
    try:
        configured_floor = cfg.get("final_risk_multiplier_floor", 0.20)
        if configured_floor is None:
            configured_floor = 0.20
        floor = max(
            0.0,
            min(1.0, float(configured_floor)),
        )
    except (TypeError, ValueError):
        floor = 0.20
    return max(multiplier, floor)


def _utbreakout_stale_signal_multiplier(cfg, ut_signal_age, max_signal_age):
    """Return the configured soft-reduction multiplier for an aged UT signal."""
    cfg = cfg or {}
    try:
        age = max(0.0, float(ut_signal_age))
    except (TypeError, ValueError):
        return 1.0
    try:
        max_age = max(1.0, float(max_signal_age))
    except (TypeError, ValueError):
        max_age = 1.0
    if age <= max_age:
        return 1.0

    stale_ratio = age / max_age
    if stale_ratio >= 6.0:
        key, default = "bias_continuation_stale_reduce_6x", 0.45
    elif stale_ratio >= 3.0:
        key, default = "bias_continuation_stale_reduce_3x", 0.55
    elif stale_ratio >= 1.5:
        key, default = "bias_continuation_stale_reduce_1_5x", 0.65
    else:
        key, default = "bias_continuation_stale_reduce_1x", 0.75
    try:
        return max(0.0, min(1.0, float(cfg.get(key, default) or default)))
    except (TypeError, ValueError):
        return default


def apply_profit_opportunity_effective_overrides(cfg):
    """Single source of truth for the EV Adaptive live config.

    Apply this after every merge boundary and immediately before status or
    live plan rendering so persisted config and legacy Set params cannot
    restore the superseded many-Set profile.
    """
    if not isinstance(cfg, dict):
        return cfg

    cfg.update({
        "effective_profile_version": UTBREAKOUT_EFFECTIVE_PROFILE_VERSION,
        "ev_adaptive_enabled": True,
        "legacy_sets_research_only": True,
        "utbreak_entry_relaxation_mode": "balanced",
        "entry_relaxation_balanced_score_buffer": 1.5,
        "entry_relaxation_active_score_buffer": 3.0,
        "entry_relaxation_balanced_probability_buffer": 0.005,
        "entry_relaxation_active_probability_buffer": 0.010,

        # AUTO selection
        "selection_mode": "auto",
        "auto_select_enabled": True,
        "live_auto_set_whitelist_enabled": True,
        "live_auto_set_whitelist": [64],
        "active_set_id": 64,
        "profile": "set64",
        "safe_live_default_set_id": 64,
        "auto_block_on_weak_margin_live": False,
        "auto_multiple_testing_penalty_enabled": False,

        # Set64 is only a stable identity. EV Adaptive is the alpha gate.
        "selected_set_core_filter_hard_block_enabled": False,
        "set_filter_soft_fail_enabled": False,

        # Candidate discovery remains permissive; EV Adaptive makes the final decision.
        "bias_continuation_min_volume_ratio": 0.40,
        "bias_continuation_15m_min_volume_ratio": 0.45,
        "bias_continuation_min_adaptive_tf_score": 30.0,
        "bias_continuation_15m_min_adaptive_tf_score": 32.0,
        "bias_continuation_min_adx": 10.0,
        "bias_continuation_15m_min_adx": 11.0,
        "bias_continuation_max_signal_age_candles": 10,
        "bias_continuation_15m_max_signal_age_candles": 10,
        "bias_continuation_stale_soft_reduce_enabled": True,
        "bias_continuation_stale_reduce_1x": 0.75,
        "bias_continuation_stale_reduce_1_5x": 0.65,
        "bias_continuation_stale_reduce_3x": 0.55,
        "bias_continuation_stale_reduce_6x": 0.45,

        # Legacy quality scores remain diagnostic features, not stacked hard gates.
        "trend_health_hard_block_below": 12.0,
        "trend_health_reduce_below": 35.0,
        "trend_health_full_score": 62.0,
        "trend_health_min_multiplier": 0.55,
        "strategy_quality_hard_block_below": 8.0,
        "strategy_quality_reduce_below": 25.0,
        "strategy_quality_full_score": 60.0,
        "strategy_quality_min_multiplier": 0.55,
        "strategy_adaptive_min_risk_multiplier": 0.50,
        "quality_score_v2_block_below": 12.0,
        "quality_score_v2_reduce_below": 40.0,
        "quality_score_v2_15m_block_below": 12.0,
        "quality_score_v2_15m_reduce_below": 40.0,
        "quality_score_v2_min_risk_multiplier": 0.60,
        "quality_score_v2_long_block_below": 12.0,
        "quality_score_v2_long_reduce_below": 40.0,
        "quality_score_v2_long_15m_block_below": 12.0,
        "quality_score_v2_long_15m_reduce_below": 40.0,
        "quality_score_v2_short_block_below": 16.0,
        "quality_score_v2_short_reduce_below": 45.0,
        "quality_score_v2_short_15m_block_below": 16.0,
        "quality_score_v2_short_15m_reduce_below": 45.0,
        "market_quality_long_hard_block_on_multi_adverse_enabled": True,
        "market_quality_long_multi_adverse_min_reasons": 4,
        "market_quality_long_multi_adverse_max_multiplier": 0.30,
        "market_quality_min_risk_multiplier": 0.0,
        "final_risk_multiplier_floor": 0.0,
        "entry_quality_gate_enabled": True,
        "entry_quality_gate_min_final_risk_multiplier": 0.45,
        "entry_quality_gate_long_min_final_risk_multiplier": 0.50,
        "entry_quality_gate_short_min_final_risk_multiplier": 0.45,
        "entry_quality_gate_hard_market_multiplier_below": 0.30,
        "entry_quality_gate_min_ev_score": 66.0,
        "entry_quality_gate_min_ev_probability": 0.54,
        "entry_quality_gate_min_ev_net_expectancy_r": 0.30,
        "entry_quality_gate_min_ev_mtf_votes": 2,
        "entry_quality_gate_min_profit_alpha_score": 68.0,
        "entry_quality_gate_min_profit_alpha_probability": 0.555,
        "entry_edge_enabled": True,
        "entry_edge_min_score": 68.0,
        "entry_edge_long_min_score": 69.0,
        "entry_edge_short_min_score": 68.0,
        "entry_edge_min_probability": 0.555,
        "entry_edge_long_min_probability": 0.560,
        "entry_edge_short_min_probability": 0.555,
        "entry_edge_min_net_expectancy_r": 0.14,
        "profit_alpha_enabled": True,
        "profit_alpha_min_score": 68.0,
        "profit_alpha_long_min_score": 69.0,
        "profit_alpha_short_min_score": 68.0,
        "profit_alpha_min_probability": 0.555,
        "profit_alpha_long_min_probability": 0.560,
        "profit_alpha_short_min_probability": 0.555,
        "profit_alpha_opposite_regime_score_add": 5.0,
        "profit_alpha_opposite_regime_probability_add": 0.010,
        "profit_alpha_stale_signal_max_age_bars": 8.0,
        "profit_alpha_stale_signal_reaccel_min_range": 1.10,
        "profit_alpha_stale_signal_reaccel_min_volume": 1.00,
        "profit_alpha_derivatives_multi_adverse_block_count": 3,
        "profit_alpha_derivatives_multi_adverse_strong_count": 2,
        "profit_alpha_meta_min_samples": 8,
        "direction_engine_min_score": 62.0,
        "direction_engine_opposite_regime_min_score": 68.0,
        "entry_type_max_chase_extension_atr": 2.35,
        "entry_type_pullback_extension_atr": 1.35,
        "entry_type_breakout_min_range": 1.12,
        "entry_type_sweep_wick_ratio": 0.38,
        "exit_meta_min_samples": 8,
        "exit_meta_expectancy_reduce_below": 0.0,
        "structure_stop_lookback_bars": 5,
        "structure_stop_buffer_atr": 0.28,
        "take_profit_front_run_atr": 0.14,
        "take_profit_front_run_pct": 0.055,
        "soft_stop_enabled": True,
        "soft_stop_confirm_bars": 2,
        "near_miss_tp_enabled": True,
        "near_miss_tp_arm_ratio": 0.86,
        "near_miss_tp_lock_r": 0.28,
        "near_miss_tp_rejection_atr": 0.12,
        "market_regime_engine_enabled": True,
        "market_regime_opposite_risk_multiplier": 0.62,
        "market_regime_chop_risk_multiplier": 0.72,
        "market_regime_high_vol_risk_multiplier": 0.58,
        "market_regime_block_extreme_chaos": True,
        "data_quality_engine_enabled": True,
        "data_quality_min_derivative_sources": 2,
        "data_quality_block_on_stale_feed": True,
        "data_quality_max_feed_age_sec": 240.0,
        "execution_quality_engine_enabled": True,
        "execution_quality_max_spread_pct": 0.12,
        "execution_quality_soft_spread_pct": 0.07,
        "execution_quality_min_depth_usdt": 25000.0,
        "protection_health_engine_enabled": True,
        "protection_health_require_plan_fields": False,
        "protection_health_execution_gate_enabled": True,
        "signal_attribution_engine_enabled": True,
        "strategy_replay_engine_enabled": True,
        "overfit_governance_enabled": True,
        "overfit_governance_hard_block_enabled": False,
        "overfit_min_samples": 12,
        "overfit_warmup_risk_multiplier": 0.92,
        "overfit_expectancy_block_below": -0.12,
        "overfit_oos_expectancy_block_below": -0.05,
        "overfit_min_profit_factor": 0.92,
        "overfit_max_pbo": 0.65,
        "overfit_multiple_testing_trials": 24,
        "overfit_walk_forward_train_size": 20,
        "overfit_walk_forward_test_size": 10,
        "overfit_walk_forward_purge_size": 1,
        "overfit_min_oos_windows": 2,
        "profit_alpha_follow_through_enabled": True,
        "profit_alpha_default_follow_through_bars": 3,
        "profit_alpha_default_follow_through_min_mfe_r": 0.35,
        "profit_alpha_default_early_exit_max_mae_r": 0.75,
        "profit_alpha_stall_exit_max_current_r": 0.0,
        "ev_time_stop_max_current_r": 0.0,
        "time_stop_max_current_r": 0.0,
        "aggressive_growth_enabled": False,
        "aggressive_growth_pyramiding_enabled": False,
        "utbreakout_recent_loss_cooldown_enabled": True,
        "utbreakout_recent_loss_cooldown_seconds": UTBREAKOUT_RECENT_LOSS_COOLDOWN_SECONDS,
        "utbreakout_recent_loss_cooldown_min_loss_usdt": UTBREAKOUT_RECENT_LOSS_COOLDOWN_MIN_LOSS_USDT,

        # Cost-aware EV gate.
        "ev_min_entry_score": 62.0,
        "ev_min_net_expectancy_r": 0.14,
        "ev_entry_fee_rate_pct": 0.04,
        "ev_exit_fee_rate_pct": 0.04,
        "ev_slippage_rate_pct_each_side": 0.02,
        "ev_funding_buffer_pct": 0.01,
        "ev_cost_safety_multiplier": 1.25,
        "ev_max_spread_pct": 0.08,
        "ev_high_vol_atr_pct": 1.50,
        "ev_extreme_atr_pct": 2.50,
        "atr_threshold_reference_timeframe": "15m",
        "atr_threshold_timeframe_scaling_enabled": True,
        "atr_threshold_max_scale": 5.0,
        "ev_panic_rebound_block_pct": 6.0,
        "ev_continuation_max_signal_age_bars": 8.0,
        "ev_continuation_reacceleration_range_min": 1.10,
        "ev_continuation_reacceleration_volume_min": 1.00,
        "ev_max_extension_atr": 2.40,
        "ev_preferred_extension_atr": 1.60,
        "ev_mtf_min_aligned": 2,
        "ev_leadership_bottom_block_pct": 15.0,
        "ev_conditional_relief_enabled": True,
        "ev_conditional_relief_risk_cap": 0.55,

        "ev_mtf_relief_enabled": True,
        "ev_mtf_relief_min_votes": 1,
        "ev_mtf_relief_min_score": 74.0,
        "ev_mtf_relief_min_adx": 26.0,
        "ev_mtf_relief_min_volume_ratio": 1.15,
        "ev_mtf_relief_min_efficiency": 0.28,

        "ev_stale_relief_enabled": True,
        "ev_stale_relief_max_age_bars": 24.0,
        "ev_stale_relief_min_score": 76.0,
        "ev_stale_relief_min_adx": 26.0,
        "ev_stale_relief_min_volume_ratio": 1.18,
        "ev_stale_relief_requires_reacceleration": True,

        "ev_no_edge_relief_enabled": True,
        "ev_no_edge_relief_min_score": 74.0,
        "ev_no_edge_relief_min_adx": 25.0,
        "ev_no_edge_relief_min_volume_ratio": 1.16,
        "ev_no_edge_relief_min_efficiency": 0.27,
        "ev_no_edge_relief_min_range_expansion": 1.10,
        "ev_short_min_entry_score": 62.0,
        "ev_short_trend_min_adx": 16.0,
        "ev_short_trend_min_volume_ratio": 0.60,
        "ev_short_no_edge_relief_min_score": 72.0,
        "ev_short_no_edge_relief_min_adx": 24.0,
        "ev_short_no_edge_relief_min_volume_ratio": 1.12,
        "ev_short_no_edge_relief_min_efficiency": 0.24,
        "ev_short_no_edge_relief_min_range_expansion": 1.08,
        "ev_short_conditional_relief_risk_cap": 0.25,
        "ev_short_relaxed_signal_risk_cap": 0.25,
        "ev_derivatives_basis_soft_pct": 0.15,
        "ev_derivatives_basis_hard_pct": 0.35,
        "ev_derivatives_multi_adverse_block_enabled": True,
        "ev_derivatives_multi_adverse_min_count": 3,
        "ev_derivatives_multi_adverse_min_hard_count": 2,
        "ev_derivatives_multi_adverse_max_risk_multiplier": 0.50,
        "ev_regime_opposition_score_add_btc": 4.0,
        "ev_regime_opposition_score_add_eth": 2.0,
        "ev_regime_strong_opposition_score_add_btc": 7.0,
        "ev_regime_opposition_risk_reduce_btc": 0.85,
        "ev_regime_opposition_risk_reduce_eth": 0.92,
        "ev_regime_strong_opposition_risk_reduce_btc": 0.70,
        "ev_regime_opposition_strong_move_pct": 1.5,
        "ev_time_stop_enabled": True,
        "ev_time_stop_bars": 8,
        "ev_time_stop_min_mfe_r": 0.45,
        "ev_mfe_profit_lock_enabled": True,
        "ev_mfe_lock_trigger_1_r": 1.50,
        "ev_mfe_lock_trigger_2_r": 2.20,
        "ev_mfe_lock_trigger_3_r": 3.20,

        # Baseline TREND exit. Strong trend and squeeze profiles override this
        # after the EV decision and again at the final live-order boundary.
        "fixed_take_profit_enabled": True,
        "partial_take_profit_enabled": True,
        "partial_take_profit_r_multiple": 1.00,
        "partial_take_profit_ratio": 0.25,
        "second_take_profit_enabled": True,
        "second_take_profit_r_multiple": 2.40,
        "second_take_profit_ratio": 0.35,
        "runner_pct": 0.40,
        "live_tp_ladder_mode": "tp1_tp2_full_exit",
        "live_tp1_pct": 0.25,
        "live_tp2_pct": 0.75,
        "live_tp1_rr": 1.00,
        "live_tp2_rr": 2.40,
        "live_min_tp2_rr": 2.00,
        "tp_split_failure_policy": "single_tp2_with_warning",
        "dynamic_take_profit_enabled": False,
        "atr_trailing_enabled": True,
        "atr_trailing_activation_r": 1.10,
        "atr_trailing_multiplier": 3.00,
        "shadow_runner_exit_enabled": True,
        "runner_exit_enabled": True,
        "runner_chandelier_enabled": True,
        "runner_chandelier_multiplier": 3.00,
        "runner_chandelier_multiplier_max": 4.20,

        # Keep adaptive research bounded around the EV baseline.
        "adaptive_exit_partial_r_min": 1.0,
        "adaptive_exit_partial_r_max": 1.2,
        "adaptive_exit_ratio_min": 0.25,
        "adaptive_exit_ratio_max": 0.35,
        "adaptive_exit_trailing_multiplier_min": 2.4,
        "adaptive_exit_trailing_multiplier_max": 3.5,
        "adaptive_exit_activation_r_min": 1.0,
        "adaptive_exit_activation_r_max": 1.4,

        # Bounded activity
        "max_daily_trades": 5,
        "max_consecutive_losses": 5,
    })

    cfg["take_profit_r_multiple"] = float(
        cfg.get("second_take_profit_r_multiple", 2.40) or 2.40
    )
    allowed_sets = set(cfg.get("live_auto_set_whitelist") or [])
    active_set_id = _safe_set_id(cfg.get("active_set_id") or cfg.get("profile"))
    if active_set_id not in allowed_sets:
        cfg["active_set_id"] = UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID
        cfg["profile"] = f"set{UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID}"
    return cfg


def _ev_adaptive_runtime_config(cfg):
    cfg = dict(cfg or {})
    return {
        "min_entry_score": cfg.get("ev_min_entry_score", 62.0),
        "min_net_expectancy_r": cfg.get("ev_min_net_expectancy_r", 0.14),
        "entry_fee_rate_pct": cfg.get("ev_entry_fee_rate_pct", 0.04),
        "exit_fee_rate_pct": cfg.get("ev_exit_fee_rate_pct", 0.04),
        "slippage_rate_pct_each_side": cfg.get("ev_slippage_rate_pct_each_side", 0.02),
        "funding_buffer_pct": cfg.get("ev_funding_buffer_pct", 0.01),
        "cost_safety_multiplier": cfg.get("ev_cost_safety_multiplier", 1.25),
        "max_spread_pct": cfg.get("ev_max_spread_pct", 0.08),
        "high_vol_atr_pct": cfg.get("ev_high_vol_atr_pct", 1.50),
        "extreme_atr_pct": cfg.get("ev_extreme_atr_pct", 2.50),
        "atr_threshold_reference_timeframe": cfg.get(
            "atr_threshold_reference_timeframe", "15m"
        ),
        "atr_threshold_timeframe_scaling_enabled": cfg.get(
            "atr_threshold_timeframe_scaling_enabled", True
        ),
        "atr_threshold_max_scale": cfg.get("atr_threshold_max_scale", 5.0),
        "panic_rebound_block_pct": cfg.get("ev_panic_rebound_block_pct", 6.0),
        "continuation_max_signal_age_bars": cfg.get(
            "ev_continuation_max_signal_age_bars", 8.0
        ),
        "continuation_reacceleration_range_min": cfg.get(
            "ev_continuation_reacceleration_range_min", 1.08
        ),
        "continuation_reacceleration_volume_min": cfg.get(
            "ev_continuation_reacceleration_volume_min", 0.95
        ),
        "max_extension_atr": cfg.get("ev_max_extension_atr", 2.40),
        "preferred_extension_atr": cfg.get("ev_preferred_extension_atr", 1.60),
        "mtf_min_aligned": cfg.get("ev_mtf_min_aligned", 2),
        "leadership_bottom_block_pct": cfg.get(
            "ev_leadership_bottom_block_pct", 15.0
        ),
        "conditional_relief_enabled": cfg.get("ev_conditional_relief_enabled", True),
        "conditional_relief_risk_cap": cfg.get("ev_conditional_relief_risk_cap", 0.55),

        "mtf_relief_enabled": cfg.get("ev_mtf_relief_enabled", True),
        "mtf_relief_min_votes": cfg.get("ev_mtf_relief_min_votes", 1),
        "mtf_relief_min_score": cfg.get("ev_mtf_relief_min_score", 70.0),
        "mtf_relief_min_adx": cfg.get("ev_mtf_relief_min_adx", 26.0),
        "mtf_relief_min_volume_ratio": cfg.get("ev_mtf_relief_min_volume_ratio", 1.15),
        "mtf_relief_min_efficiency": cfg.get("ev_mtf_relief_min_efficiency", 0.28),

        "stale_relief_enabled": cfg.get("ev_stale_relief_enabled", True),
        "stale_relief_max_age_bars": cfg.get("ev_stale_relief_max_age_bars", 24.0),
        "stale_relief_min_score": cfg.get("ev_stale_relief_min_score", 73.0),
        "stale_relief_min_adx": cfg.get("ev_stale_relief_min_adx", 25.0),
        "stale_relief_min_volume_ratio": cfg.get("ev_stale_relief_min_volume_ratio", 1.15),
        "stale_relief_requires_reacceleration": cfg.get("ev_stale_relief_requires_reacceleration", True),

        "no_edge_relief_enabled": cfg.get("ev_no_edge_relief_enabled", True),
        "no_edge_relief_min_score": cfg.get("ev_no_edge_relief_min_score", 70.0),
        "no_edge_relief_min_adx": cfg.get("ev_no_edge_relief_min_adx", 24.0),
        "no_edge_relief_min_volume_ratio": cfg.get("ev_no_edge_relief_min_volume_ratio", 1.10),
        "no_edge_relief_min_efficiency": cfg.get("ev_no_edge_relief_min_efficiency", 0.27),
        "no_edge_relief_min_range_expansion": cfg.get("ev_no_edge_relief_min_range_expansion", 1.10),
        "short_min_entry_score": cfg.get("ev_short_min_entry_score", 62.0),
        "short_trend_min_adx": cfg.get("ev_short_trend_min_adx", 16.0),
        "short_trend_min_volume_ratio": cfg.get("ev_short_trend_min_volume_ratio", 0.60),
        "short_no_edge_relief_min_score": cfg.get("ev_short_no_edge_relief_min_score", 68.0),
        "short_no_edge_relief_min_adx": cfg.get("ev_short_no_edge_relief_min_adx", 23.0),
        "short_no_edge_relief_min_volume_ratio": cfg.get("ev_short_no_edge_relief_min_volume_ratio", 1.05),
        "short_no_edge_relief_min_efficiency": cfg.get("ev_short_no_edge_relief_min_efficiency", 0.24),
        "short_no_edge_relief_min_range_expansion": cfg.get("ev_short_no_edge_relief_min_range_expansion", 1.08),
        "short_conditional_relief_risk_cap": cfg.get("ev_short_conditional_relief_risk_cap", 0.30),
        "short_relaxed_signal_risk_cap": cfg.get("ev_short_relaxed_signal_risk_cap", 0.30),
        "derivatives_basis_soft_pct": cfg.get("ev_derivatives_basis_soft_pct", 0.15),
        "derivatives_basis_hard_pct": cfg.get("ev_derivatives_basis_hard_pct", 0.35),
        "derivatives_multi_adverse_block_enabled": cfg.get("ev_derivatives_multi_adverse_block_enabled", True),
        "derivatives_multi_adverse_min_count": cfg.get("ev_derivatives_multi_adverse_min_count", 3),
        "derivatives_multi_adverse_min_hard_count": cfg.get("ev_derivatives_multi_adverse_min_hard_count", 2),
        "derivatives_multi_adverse_max_risk_multiplier": cfg.get("ev_derivatives_multi_adverse_max_risk_multiplier", 0.50),
        "regime_opposition_score_add_btc": cfg.get("ev_regime_opposition_score_add_btc", 4.0),
        "regime_opposition_score_add_eth": cfg.get("ev_regime_opposition_score_add_eth", 2.0),
        "regime_strong_opposition_score_add_btc": cfg.get("ev_regime_strong_opposition_score_add_btc", 7.0),
        "regime_opposition_risk_reduce_btc": cfg.get("ev_regime_opposition_risk_reduce_btc", 0.85),
        "regime_opposition_risk_reduce_eth": cfg.get("ev_regime_opposition_risk_reduce_eth", 0.92),
        "regime_strong_opposition_risk_reduce_btc": cfg.get("ev_regime_strong_opposition_risk_reduce_btc", 0.70),
        "regime_opposition_strong_move_pct": cfg.get("ev_regime_opposition_strong_move_pct", 1.5),
    }


def _apply_ev_exit_profile(cfg, profile):
    cfg = dict(cfg or {})
    if profile is None:
        return cfg
    cfg.update({
        "fixed_take_profit_enabled": True,
        "partial_take_profit_enabled": profile.tp1_ratio > 0,
        "partial_take_profit_r_multiple": float(profile.tp1_r),
        "partial_take_profit_ratio": float(profile.tp1_ratio),
        "second_take_profit_enabled": profile.tp2_ratio > 0,
        "second_take_profit_r_multiple": float(profile.tp2_r),
        "second_take_profit_ratio": float(profile.tp2_ratio),
        "runner_pct": float(profile.runner_ratio),
        "take_profit_r_multiple": float(
            profile.tp2_r if profile.tp2_ratio > 0 else profile.tp1_r
        ),
        "dynamic_take_profit_enabled": False,
        "atr_trailing_enabled": profile.runner_ratio > 0,
        "atr_trailing_activation_r": float(profile.trailing_activation_r),
        "atr_trailing_multiplier": float(profile.trailing_atr_multiplier),
        "runner_exit_enabled": profile.runner_ratio > 0,
        "runner_chandelier_enabled": profile.runner_ratio > 0,
        "runner_chandelier_multiplier": float(profile.trailing_atr_multiplier),
        "tp1_breakeven_enabled": profile.tp1_ratio > 0,
        "tp1_breakeven_trigger_r": float(profile.tp1_r),
        "ev_exit_profile_name": str(profile.name),
        "ev_time_stop_bars": int(profile.time_stop_bars),
        "ev_time_stop_min_mfe_r": float(profile.time_stop_min_mfe_r),
    })
    return cfg


def build_utbreakout_effective_status_contract(cfg, daily_entries=None):
    """Build the authoritative profile lines shown in every UTBreak status."""
    source_cfg = dict(cfg or {})
    try:
        requested_daily_limit = int(float(source_cfg.get('max_daily_trades', 5) or 5))
    except (TypeError, ValueError):
        requested_daily_limit = 5
    effective = apply_profit_opportunity_effective_overrides(source_cfg)
    if requested_daily_limit in {5, 10}:
        effective['max_daily_trades'] = requested_daily_limit
    lines = [
        f"Effective Profile: {effective.get('effective_profile_version', 'UNKNOWN')}",
        "Strategy Router: Entry Edge (UT trigger + EV/Alpha integrated)",
        f"Effective TP2: {float(effective.get('second_take_profit_r_multiple', 2.40) or 2.40):.2f}R",
        (
            "Effective volume: "
            f"base {float(effective.get('bias_continuation_min_volume_ratio', 0.40) or 0.40):.2f} / "
            f"15m {float(effective.get('bias_continuation_15m_min_volume_ratio', 0.45) or 0.45):.2f}"
        ),
        (
            "익절 계획: "
            f"TP1 {float(effective.get('partial_take_profit_r_multiple', 1.00) or 1.00):.2f}R"
            f"({float(effective.get('partial_take_profit_ratio', 0.30) or 0.30):.0%}) / "
            f"TP2 {float(effective.get('second_take_profit_r_multiple', 2.40) or 2.40):.2f}R"
            f"({float(effective.get('second_take_profit_ratio', 0.40) or 0.40):.0%})"
        ),
    ]
    if daily_entries is not None:
        try:
            entry_count = int(daily_entries)
        except (TypeError, ValueError):
            entry_count = 0
        lines.append(
            f"일일 리스크: trades {entry_count}/{int(effective.get('max_daily_trades', 5) or 5)}"
        )
    return lines


def enforce_utbreakout_effective_status_contract(text, cfg, daily_entries=None):
    """Replace stale profile summary lines at the final Telegram render boundary."""
    source_cfg = dict(cfg or {})
    try:
        requested_daily_limit = int(float(source_cfg.get('max_daily_trades', 5) or 5))
    except (TypeError, ValueError):
        requested_daily_limit = 5
    effective = apply_profit_opportunity_effective_overrides(source_cfg)
    if requested_daily_limit in {5, 10}:
        effective['max_daily_trades'] = requested_daily_limit
    raw_lines = str(text or "").splitlines()
    stale_prefixes = (
        "Effective Profile:",
        "Strategy Router:",
        "Effective TP2:",
        "Effective volume:",
        "익절 계획:",
        "일일 리스크: trades ",
    )
    filtered = []
    for raw_line in raw_lines:
        line = str(raw_line)
        if line.strip().startswith(stale_prefixes):
            continue
        if "Bias continuation" in line:
            line = re.sub(
                r"volume ratio ([0-9.]+)<0\.(?:75|80)",
                r"volume ratio \1<0.45",
                line,
            )
        exit_match = re.search(
            r"exit partial ([0-9.]+)%@([0-9.]+)R, trail ([0-9.]+)ATR from ([0-9.]+)R",
            line,
        )
        if exit_match:
            ratio, partial_r, trailing_mult, activation_r = map(float, exit_match.groups())
            if not (
                20.0 <= ratio <= 30.0
                and 1.0 <= partial_r <= 1.2
                and 2.7 <= trailing_mult <= 3.2
                and 1.0 <= activation_r <= 1.3
            ):
                prefix = line[:exit_match.start()]
                line = (
                    f"{prefix}exit partial 20-30%@1.00-1.20R, "
                    "trail 2.70-3.20ATR from 1.00-1.30R "
                    "(effective profile bounds)"
                )
        selected_set_match = re.match(r"(\s*선택 Set:\s*)Set(\d+)(.*)", line)
        if selected_set_match:
            selected_set_id = int(selected_set_match.group(2))
            allowed_sets = set(effective.get("live_auto_set_whitelist") or [])
            if selected_set_id not in allowed_sets:
                fallback_set = int(
                    effective.get("active_set_id", UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID)
                    or UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID
                )
                line = (
                    f"{selected_set_match.group(1)}Set{fallback_set} "
                    f"(effective AUTO fallback; legacy Set{selected_set_id} blocked)"
                )
        filtered.append(line)
    contract = build_utbreakout_effective_status_contract(effective, daily_entries)
    if not filtered:
        return "\n".join(contract)
    return "\n".join([filtered[0], *contract, *filtered[1:]])


def apply_stable_utbreak_final_overrides(cfg):
    """Apply final UTBreak config overrides after selected Set params are merged.

    This function must be the single source of truth for live entry evaluation
    and Telegram condition status. Do not read raw Set params directly for
    status/entry thresholds after this point.
    """
    if not isinstance(cfg, dict):
        return cfg

    cfg.update({
        "runtime_profile": UTBREAKOUT_RUNTIME_PROFILE,

        # Stable execution design
        "auto_timeframes": ["15m", "30m", "1h"],
        "adaptive_timeframes": ["15m", "30m", "1h"],
        "entry_timeframe": "15m",
        "exit_timeframe": "15m",
        "htf_timeframe": "1h",

        # Prevent frequent TF switching
        "adaptive_timeframe_min_score": 38.0,
        "adaptive_timeframe_switch_margin": 10.0,
        "adaptive_timeframe_min_hold_candles": 6,

        # Legacy Set variants are research-only. Live uses Set64 as a stable shell.
        "set_selection_hysteresis_enabled": False,
        "set_complexity_penalty_enabled": False,
        "selected_set_core_filter_hard_block_enabled": False,
        "set_filter_soft_fail_enabled": False,
        "live_auto_set_whitelist_enabled": True,
        "live_auto_set_whitelist": [64],
        "safe_live_default_set_id": 64,
        "auto_block_on_weak_margin_live": False,
        "auto_multiple_testing_penalty_enabled": False,

        "utbreak_entry_relaxation_mode": "balanced",
        "entry_relaxation_balanced_score_buffer": 1.5,
        "entry_relaxation_active_score_buffer": 3.0,
        "entry_relaxation_balanced_probability_buffer": 0.005,
        "entry_relaxation_active_probability_buffer": 0.010,

        # Bias continuation thresholds
        "bias_continuation_min_volume_ratio": 0.40,
        "bias_continuation_15m_min_volume_ratio": 0.45,
        "bias_continuation_min_adaptive_tf_score": 30.0,
        "bias_continuation_15m_min_adaptive_tf_score": 32.0,
        "bias_continuation_min_adx": 10.0,
        "bias_continuation_15m_min_adx": 11.0,
        "bias_continuation_max_signal_age_candles": 10,
        "bias_continuation_15m_max_signal_age_candles": 10,

        # Hard block should be reserved for extreme bad states only
        "trend_health_hard_block_below": 12.0,
        "trend_health_reduce_below": 35.0,
        "trend_health_full_score": 62.0,
        "trend_health_min_multiplier": 0.55,

        "strategy_quality_hard_block_below": 8.0,
        "strategy_quality_reduce_below": 25.0,
        "strategy_quality_full_score": 60.0,
        "strategy_quality_min_multiplier": 0.55,
        "strategy_adaptive_min_risk_multiplier": 0.50,

        "quality_score_v2_block_below": 12.0,
        "quality_score_v2_reduce_below": 40.0,
        "quality_score_v2_15m_block_below": 12.0,
        "quality_score_v2_15m_reduce_below": 40.0,
        "quality_score_v2_min_risk_multiplier": 0.60,

        "quality_score_v2_long_block_below": 12.0,
        "quality_score_v2_long_reduce_below": 40.0,
        "quality_score_v2_long_15m_block_below": 12.0,
        "quality_score_v2_long_15m_reduce_below": 40.0,

        "quality_score_v2_short_block_below": 16.0,
        "quality_score_v2_short_reduce_below": 45.0,
        "quality_score_v2_short_15m_block_below": 16.0,
        "quality_score_v2_short_15m_reduce_below": 45.0,

        # Accepted risk is selected by the EV router; never raise a weak signal
        # back to a minimum size after reductions.
        "market_quality_long_hard_block_on_multi_adverse_enabled": False,
        "market_quality_long_multi_adverse_min_reasons": 5,
        "market_quality_long_multi_adverse_max_multiplier": 0.35,
        "market_quality_min_risk_multiplier": 0.0,
        "final_risk_multiplier_floor": 0.0,
        "entry_quality_gate_enabled": True,
        "entry_quality_gate_min_final_risk_multiplier": 0.45,
        "entry_quality_gate_long_min_final_risk_multiplier": 0.50,
        "entry_quality_gate_short_min_final_risk_multiplier": 0.45,
        "entry_quality_gate_hard_market_multiplier_below": 0.25,
        "entry_quality_gate_min_ev_score": 66.0,
        "entry_quality_gate_min_ev_probability": 0.54,
        "entry_quality_gate_min_ev_net_expectancy_r": 0.30,
        "entry_quality_gate_min_ev_mtf_votes": 2,
        "entry_quality_gate_min_profit_alpha_score": 68.0,
        "entry_quality_gate_min_profit_alpha_probability": 0.555,
        "entry_edge_enabled": True,
        "entry_edge_min_score": 68.0,
        "entry_edge_long_min_score": 69.0,
        "entry_edge_short_min_score": 68.0,
        "entry_edge_min_probability": 0.555,
        "entry_edge_long_min_probability": 0.560,
        "entry_edge_short_min_probability": 0.555,
        "entry_edge_min_net_expectancy_r": 0.14,

        # Set32 keeps structure confirmation with more tolerant flow inputs.
        "set32_min_relative_volume": 1.15,
        "set32_require_direction_candle": True,
        "set32_require_ema50_side": True,
        "set32_require_orderflow_confirmation": True,
        "set32_orderflow_min_samples": 2,
        "set32_min_taker_ratio_long": 0.97,
        "set32_max_taker_ratio_short": 1.03,
        "set32_max_spread_pct": 0.09,

        # Short remains stricter than long, but not dead
        "short_adx_threshold": 20.0,
        "short_dmi_min_gap": 2.0,
        "short_risk_multiplier": 0.60,

        # Exit profile
        "partial_take_profit_r_multiple": 1.00,
        "partial_take_profit_ratio": 0.25,
        "second_take_profit_r_multiple": 2.40,
        "second_take_profit_ratio": 0.35,
        "runner_pct": 0.40,
        "dynamic_take_profit_enabled": False,
        "atr_trailing_activation_r": 1.10,
        "atr_trailing_multiplier": 3.00,
        "atr_trailing_enabled": True,
        "shadow_runner_exit_enabled": True,
        "runner_exit_enabled": True,
        "runner_chandelier_enabled": True,
        "runner_chandelier_multiplier": 3.00,
        "runner_chandelier_multiplier_max": 4.20,
        "adaptive_exit_partial_r_min": 1.0,
        "adaptive_exit_partial_r_max": 1.2,
        "adaptive_exit_ratio_min": 0.20,
        "adaptive_exit_ratio_max": 0.35,
        "adaptive_exit_trailing_multiplier_min": 3.0,
        "adaptive_exit_trailing_multiplier_max": 4.0,
        "adaptive_exit_activation_r_min": 1.4,
        "adaptive_exit_activation_r_max": 1.8,

        "profit_alpha_enabled": True,
        "profit_alpha_min_score": 68.0,
        "profit_alpha_long_min_score": 69.0,
        "profit_alpha_short_min_score": 68.0,
        "profit_alpha_min_probability": 0.555,
        "profit_alpha_long_min_probability": 0.560,
        "profit_alpha_short_min_probability": 0.555,
        "profit_alpha_opposite_regime_score_add": 5.0,
        "profit_alpha_opposite_regime_probability_add": 0.010,
        "profit_alpha_stale_signal_max_age_bars": 8.0,
        "profit_alpha_stale_signal_reaccel_min_range": 1.10,
        "profit_alpha_stale_signal_reaccel_min_volume": 1.00,
        "profit_alpha_derivatives_multi_adverse_block_count": 3,
        "profit_alpha_derivatives_multi_adverse_strong_count": 2,
        "profit_alpha_meta_min_samples": 8,
        "direction_engine_min_score": 62.0,
        "direction_engine_opposite_regime_min_score": 68.0,
        "entry_type_max_chase_extension_atr": 2.35,
        "entry_type_pullback_extension_atr": 1.35,
        "entry_type_breakout_min_range": 1.12,
        "entry_type_sweep_wick_ratio": 0.38,
        "exit_meta_min_samples": 8,
        "exit_meta_expectancy_reduce_below": 0.0,
        "structure_stop_lookback_bars": 5,
        "structure_stop_buffer_atr": 0.28,
        "take_profit_front_run_atr": 0.14,
        "take_profit_front_run_pct": 0.055,
        "soft_stop_enabled": True,
        "soft_stop_confirm_bars": 2,
        "near_miss_tp_enabled": True,
        "near_miss_tp_arm_ratio": 0.86,
        "near_miss_tp_lock_r": 0.28,
        "near_miss_tp_rejection_atr": 0.12,
        "market_regime_engine_enabled": True,
        "market_regime_opposite_risk_multiplier": 0.62,
        "market_regime_chop_risk_multiplier": 0.72,
        "market_regime_high_vol_risk_multiplier": 0.58,
        "market_regime_block_extreme_chaos": True,
        "data_quality_engine_enabled": True,
        "data_quality_min_derivative_sources": 2,
        "data_quality_block_on_stale_feed": True,
        "data_quality_max_feed_age_sec": 240.0,
        "execution_quality_engine_enabled": True,
        "execution_quality_max_spread_pct": 0.12,
        "execution_quality_soft_spread_pct": 0.07,
        "execution_quality_min_depth_usdt": 25000.0,
        "protection_health_engine_enabled": True,
        "protection_health_require_plan_fields": False,
        "protection_health_execution_gate_enabled": True,
        "signal_attribution_engine_enabled": True,
        "strategy_replay_engine_enabled": True,
        "overfit_governance_enabled": True,
        "overfit_governance_hard_block_enabled": False,
        "overfit_min_samples": 12,
        "overfit_warmup_risk_multiplier": 0.92,
        "overfit_expectancy_block_below": -0.12,
        "overfit_oos_expectancy_block_below": -0.05,
        "overfit_min_profit_factor": 0.92,
        "overfit_max_pbo": 0.65,
        "overfit_multiple_testing_trials": 24,
        "overfit_walk_forward_train_size": 20,
        "overfit_walk_forward_test_size": 10,
        "overfit_walk_forward_purge_size": 1,
        "overfit_min_oos_windows": 2,
        "profit_alpha_follow_through_enabled": True,
        "profit_alpha_default_follow_through_bars": 3,
        "profit_alpha_default_follow_through_min_mfe_r": 0.35,
        "profit_alpha_default_early_exit_max_mae_r": 0.75,
        "profit_alpha_stall_exit_max_current_r": 0.0,
        "ev_time_stop_max_current_r": 0.0,
        "time_stop_max_current_r": 0.0,

        "max_daily_trades": 5,
        "max_consecutive_losses": 5,
    })
    cfg = apply_profit_opportunity_effective_overrides(cfg)
    return cfg


def _build_utbreakout_set(
    set_id,
    family,
    name,
    description,
    regime,
    pros,
    cons,
    frequency_impact,
    indicators,
    entry_filters=None,
    params=None,
):
    status = 'active' if int(set_id) <= UTBREAKOUT_ACTIVE_SET_MAX else 'planned'
    return {
        'id': int(set_id),
        'key': f"set{int(set_id)}",
        'family': family,
        'name': name,
        'status': status,
        'description': description,
        'regime': regime,
        'pros': pros,
        'cons': cons,
        'frequency_impact': frequency_impact,
        'indicators': list(indicators or []),
        'entry_filters': list(entry_filters or []),
        'params': dict(params or {}),
    }


def build_utbreakout_set_registry():
    base_params = {
        'utbot_key_value': 2.5,
        'utbot_atr_period': 14,
        'stop_atr_multiplier': 1.5,
        'take_profit_r_multiple': 3.50,
        'fixed_take_profit_enabled': True,
        'partial_take_profit_enabled': True,
        'partial_take_profit_r_multiple': 1.00,
        'partial_take_profit_ratio': 0.20,
        'second_take_profit_enabled': True,
        'second_take_profit_r_multiple': 3.50,
        'second_take_profit_ratio': 0.40,
        'enable_tp2_fallback_close': False,
        'tp2_fallback_confirm_loops': 2,
        'tp2_fallback_use_market': True,
        'atr_trailing_enabled': True,
        'atr_trailing_multiplier': 3.50,
        'atr_trailing_activation_r': 1.60,
        'atr_trailing_breakeven_enabled': True,
        'short_conservative_enabled': True,
        'short_risk_multiplier': 0.5,
        'short_adx_threshold': 25.0,
        'short_dmi_min_gap': 4.0,
        'short_require_htf_supertrend': True,
        'short_require_entry_ema_downtrend': True,
        'short_require_momentum_downtrend': True,
        'bias_continuation_enabled': True,
        'bias_continuation_risk_multiplier': 0.65,
        'bias_continuation_15m_risk_multiplier': 0.50,
        'bias_continuation_max_signal_age_candles': 10,
        'bias_continuation_15m_max_signal_age_candles': 10,
        'bias_continuation_min_adx': 10.0,
        'bias_continuation_15m_min_adx': 11.0,
        'bias_continuation_min_volume_ratio': 0.40,
        'bias_continuation_15m_min_volume_ratio': 0.45,
        'bias_continuation_max_extension_atr': 1.60,
        'bias_continuation_15m_max_extension_atr': 1.50,
        'bias_continuation_min_adaptive_tf_score': 30.0,
        'bias_continuation_15m_min_adaptive_tf_score': 32.0,
        'quality_score_v2_enabled': True,
        'quality_score_v2_block_below': 12.0,
        'quality_score_v2_reduce_below': 40.0,
        'quality_score_v2_full_score': 82.0,
        'quality_score_v2_min_risk_multiplier': 0.60,
        'quality_score_v2_15m_block_below': 12.0,
        'quality_score_v2_15m_reduce_below': 40.0,
        'quality_score_v2_long_block_below': 12.0,
        'quality_score_v2_long_reduce_below': 40.0,
        'quality_score_v2_long_15m_block_below': 12.0,
        'quality_score_v2_long_15m_reduce_below': 40.0,
        'quality_score_v2_short_block_below': 16.0,
        'quality_score_v2_short_reduce_below': 45.0,
        'quality_score_v2_short_15m_block_below': 16.0,
        'quality_score_v2_short_15m_reduce_below': 45.0,
        'feature_score_enabled': True,
        'feature_score_block_below': 55.0,
        'feature_score_reduce_below': 65.0,
        'feature_score_min_risk_multiplier': 0.60,
        'rolling_ofi_window': 5,
        'rolling_ofi_min_abs': 3.0,
        'rolling_ofi_taker_long_min': 1.03,
        'rolling_ofi_taker_short_max': 0.97,
        'rolling_ofi_spread_max_pct': 0.05,
        'prediction_min_depth_usdt': 50000.0,
        'oi_z_min': 0.75,
        'oi_acceleration_min': 0.0,
        'funding_long_max': 0.0008,
        'funding_short_min': -0.0008,
        'long_short_long_max': 1.85,
        'long_short_short_min': 0.55,
        'basis_long_max_pct': 0.20,
        'basis_short_min_pct': -0.20,
        'squeeze_lookback': 80,
        'squeeze_percentile_max': 25.0,
        'squeeze_release_range_min': 1.05,
        'squeeze_volume_ratio_min': 1.20,
        'squeeze_require_keltner': False,
        'dynamic_take_profit_enabled': True,
        'dynamic_tp2_strong_score': 72.0,
        'dynamic_tp2_elite_score': 82.0,
        'dynamic_tp2_base_r_multiple': 3.20,
        'dynamic_tp2_strong_r_multiple': 5.00,
        'dynamic_tp2_elite_r_multiple': 7.00,
        'tp1_breakeven_enabled': True,
        'tp1_breakeven_trigger_r': 1.5,
        'tp1_breakeven_offset_r': 0.03,
        'tp1_breakeven_wait_for_partial': True,
        'tp1_breakeven_qty_tolerance': 0.08,
        'market_quality_enabled': True,
        'regime_filter_enabled': False,
        'meta_sizing_enabled': False,
        'portfolio_risk_enabled': False,
        'drawdown_brake_enabled': False,
        'adaptive_exit_v2_enabled': False,
        'walk_forward_report_enabled': False,
        'advanced_alpha_engine_enabled': False,
        'advanced_alpha_paper_testnet_default_enabled': True,
        'adaptive_ladder_tp_enabled': False,
        'macro_guard_enabled': False,
        'engine_kill_switch_enabled': True,
        'live_parity_signal_enabled': False,
        'market_quality_data_required': False,
        'market_quality_min_risk_multiplier': 0.55,
        'market_quality_high_atr_pct': 1.5,
        'market_quality_extreme_atr_pct': 2.5,
        'market_quality_adverse_funding_soft': 0.0006,
        'market_quality_adverse_funding_hard': 0.0015,
        'market_quality_regime_enabled': True,
        'market_quality_regime_symbols': ['BTC/USDT', 'ETH/USDT'],
        'market_quality_regime_timeframe': '4h',
        'market_quality_regime_strong_move_pct': 1.5,
        'shadow_triple_barrier_enabled': True,
        'shadow_triple_barrier_max_bars': 24,
        'shadow_runner_exit_enabled': True,
        'shadow_runner_max_bars': 48,
        'runner_exit_enabled': True,
        'runner_chandelier_enabled': True,
        'runner_chandelier_lookback': 22,
        'runner_structure_lookback': 5,
        'runner_structure_buffer_atr': 0.20,
        'runner_dynamic_multiplier_enabled': True,
        'runner_chandelier_multiplier': 3.80,
        'runner_chandelier_multiplier_min': 1.4,
        'runner_chandelier_multiplier_max': 4.50,
        'runner_mfe_tighten_r': 3.0,
        'runner_mfe_tighten_delta': 0.20,
        'trend_health_enabled': True,
        'trend_health_directional_lookback': 12,
        'trend_health_volatility_long_length': 50,
        'trend_health_hard_block_below': 12.0,
        'trend_health_reduce_below': 35.0,
        'trend_health_full_score': 62.0,
        'trend_health_min_multiplier': 0.55,
        'strategy_quality_enabled': True,
        'strategy_quality_hard_block_below': 8.0,
        'strategy_quality_reduce_below': 25.0,
        'strategy_quality_full_score': 60.0,
        'strategy_quality_min_multiplier': 0.55,
        'strategy_quality_regression_lookback': 24,
        'strategy_quality_hurst_lookback': 64,
        'strategy_quality_rebound_lookback': 12,
        'adaptive_exit_enabled': True,
        'adaptive_exit_min_samples': 8,
        'adaptive_exit_lookback_days': 14,
        'adaptive_exit_partial_r_min': 1.0,
        'adaptive_exit_partial_r_max': 1.2,
        'adaptive_exit_ratio_min': 0.20,
        'adaptive_exit_ratio_max': 0.35,
        'adaptive_exit_trailing_multiplier_min': 3.0,
        'adaptive_exit_trailing_multiplier_max': 4.0,
        'adaptive_exit_activation_r_min': 1.4,
        'adaptive_exit_activation_r_max': 1.8,
        'volatility_targeting_enabled': True,
        'volatility_target_atr_pct': 1.0,
        'volatility_target_min_multiplier': 0.25,
        'meta_labeling_enabled': True,
        'meta_labeling_min_samples': 12,
        'meta_labeling_min_multiplier': 0.5,
        'strategy_adaptive_min_risk_multiplier': 0.50,
        'short_asymmetry_enabled': True,
        'short_partial_take_profit_r_delta': 0.20,
        'short_partial_take_profit_ratio_add': 0.10,
        'short_atr_trailing_multiplier_delta': 0.25,
        'short_atr_trailing_activation_r_delta': 0.20,
        'min_risk_reward': 2.0,
    }
    rows = [
        (1, 'UT Core', 'UT only', 'UTBot 방향 유지 상태만 진입 후보로 쓰는 가장 단순한 세트입니다.', '애매한 장세에서 진입 자체가 사라지는 것을 피하고 싶을 때', '진입 빈도가 가장 높고 구조가 단순함', '횡보장 false signal 방어는 약함', '매우 많음', ['UTBot'], [], {'utbot_key_value': 2.0, 'utbot_atr_period': 10}),
        (2, 'UT Core', 'UT + ATR guard', 'UTBot에 최소/최대 변동성 가드만 붙입니다.', '변동성이 너무 죽었거나 과열된 구간만 피하고 싶을 때', '진입을 크게 줄이지 않으면서 위험 구간만 제거', '추세 방향 품질 검증은 약함', '많음', ['UTBot', 'ATR%'], ['atr_guard'], {'utbot_key_value': 2.0, 'utbot_atr_period': 10}),
        (3, 'UT Core', 'UT + HTF trend', '1시간 EMA50/EMA200 방향이 UT 방향과 맞을 때만 허용합니다.', '상위 추세가 비교적 뚜렷한 장', '역방향 진입을 줄임', '상위 추세 전환 초반에는 늦을 수 있음', '중간', ['UTBot', '1H EMA50/200'], ['htf_trend'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (4, 'UT Core', 'UT + EMA slope', '15분 EMA50 기울기와 가격 위치만 확인합니다.', '짧은 추세가 막 살아나는 구간', 'HTF보다 빠르게 반응', '짧은 노이즈에 흔들릴 수 있음', '중간~많음', ['UTBot', 'EMA slope'], ['ema_slope'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (5, 'UT Core', 'UT + HTF Supertrend', '상위봉 Supertrend 방향과 UTBot 방향을 맞춥니다.', '추세 추종 성격이 강한 장', '추세장 필터로 직관적', '횡보 전환 때 늦게 반응할 수 있음', '중간', ['UTBot', 'HTF Supertrend'], ['htf_supertrend'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (6, 'Trend Strength', 'UT + ADX loose', 'ADX 20 이상이면 추세 가능성이 있다고 보고 허용합니다.', '추세 강도만 약하게 보고 싶을 때', 'Set7보다 진입이 많음', '방향성은 UTBot에 많이 의존', '중간~많음', ['UTBot', 'ADX'], ['adx_loose'], {'utbot_key_value': 2.0, 'utbot_atr_period': 10, 'adx_threshold': 20.0}),
        (7, 'Trend Strength', 'UT + ADX + DMI', 'ADX와 +DI/-DI 방향까지 함께 확인합니다.', '추세 강도와 방향이 함께 잡히는 구간', '횡보장 손실을 줄이는 데 유리', 'Set6보다 진입이 줄어듦', '중간', ['UTBot', 'ADX', 'DMI'], ['adx_dmi'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14, 'adx_threshold': 22.0}),
        (8, 'Trend Strength', 'UT + CHOP trend', 'CHOP 값이 낮아 추세성 장세일 때만 허용합니다.', '횡보가 줄고 방향성이 생기는 구간', '횡보 회피에 직접적', '강한 압축 후 초기 돌파는 놓칠 수 있음', '적음~중간', ['UTBot', 'CHOP'], ['chop_trend'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (9, 'Trend Strength', 'UT + Vortex', 'Vortex 방향성이 UTBot 방향과 맞을 때 허용합니다.', '방향 전환과 추세 지속을 같이 보고 싶을 때', 'DMI와 다른 방식의 방향 확인', '급변동 구간에서 흔들릴 수 있음', '중간', ['UTBot', 'Vortex'], ['vortex'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (10, 'Trend Strength', 'UT + Aroon', 'Aroon Up/Down으로 최근 고저점 갱신 방향을 확인합니다.', '신고가/신저가 갱신 흐름이 있는 장', '돌파성 추세 포착에 유리', '박스권에서는 잦은 전환 가능', '중간', ['UTBot', 'Aroon'], ['aroon'], {'utbot_key_value': 2.5, 'utbot_atr_period': 14}),
        (11, 'Momentum', 'UT + RSI50', 'RSI 50선을 방향성 확인용으로 쓰는 세트입니다.', '모멘텀이 선명한 장', '간단하고 해석 쉬움', 'RSI 50 근처에서 진입 감소', '중간', ['UTBot', 'RSI'], ['rsi_momentum'], {}),
        (12, 'Momentum', 'UT + MACD histogram', 'MACD 히스토그램 방향으로 모멘텀을 확인합니다.', '추세 전환 후 가속 구간', '가속도 확인에 유리', '횡보 중 잦은 반전 가능', '중간', ['UTBot', 'MACD'], ['macd_histogram'], {}),
        (13, 'Momentum', 'UT + ROC', 'Rate of Change로 단기 가격 추진력을 확인합니다.', '방향성이 빠르게 붙는 구간', '빠른 반응', '급등락 후 늦은 진입 가능', '중간~많음', ['UTBot', 'ROC'], ['roc_momentum'], {}),
        (14, 'Momentum', 'UT + CCI', 'CCI가 0선 기준으로 방향성을 보일 때 허용합니다.', '평균 대비 가격 위치가 방향성을 띨 때', '추세/과열을 같이 관찰 가능', '노이즈 구간에서는 흔들릴 수 있음', '중간', ['UTBot', 'CCI'], ['cci_direction'], {}),
        (15, 'Momentum', 'UT + Stochastic direction', '스토캐스틱 K/D 방향으로 단기 힘을 확인합니다.', '짧은 파동 추종', '민감한 진입 가능', '잦은 신호로 과매매 위험', '많음', ['UTBot', 'Stochastic'], ['stoch_direction'], {}),
        (16, 'Volatility Regime', 'UT + ATR normal', 'ATR% 정상 구간만 허용합니다.', '일반 변동성 장', '위험한 저/고변동성 회피', '기회 일부 감소', '중간~많음', ['UTBot', 'ATR%'], ['atr_guard'], {}),
        (17, 'Volatility Regime', 'UT + ATR low-vol caution', '저변동성에서는 더 보수적으로 진입합니다.', '거래량과 변동성이 낮은 장', '무의미한 진입 감소', '초기 변동성 확장 전 놓칠 수 있음', '적음', ['UTBot', 'ATR%'], ['atr_low_vol_caution'], {}),
        (18, 'Volatility Regime', 'UT + ATR high-vol reduce', '고변동성에서는 진입 축소 또는 회피를 목표로 합니다.', '뉴스성 급변동 장', '큰 손실 꼬리 방어', '강한 추세 수익 일부 포기', '적음', ['UTBot', 'ATR%'], ['atr_high_vol_reduce'], {}),
        (19, 'Volatility Regime', 'UT + BB Width expansion', '볼린저 밴드 폭 확장으로 변동성 시작을 봅니다.', '압축 후 확장 구간', '돌파 초반 감지', '가짜 확장 가능', '중간', ['UTBot', 'Bollinger BandWidth'], ['bb_width_expansion'], {}),
        (20, 'Volatility Regime', 'UT + Keltner expansion', 'ATR 기반 Keltner 채널 확장으로 추세성을 봅니다.', 'ATR 변동성 확장 구간', '변동성 기반이라 실전적', '급등락 후 늦을 수 있음', '중간', ['UTBot', 'Keltner Channel'], ['keltner_expansion'], {}),
        (21, 'Breakout', 'UT + Donchian 10', '직전 10봉 Donchian 돌파를 확인합니다.', '짧은 돌파장', 'Set22보다 빠름', '가짜 돌파 증가', '중간', ['UTBot', 'Donchian'], ['donchian_breakout'], {'donchian_length': 10}),
        (22, 'Breakout', 'UT + Donchian 20', '직전 20봉 Donchian 돌파를 확인합니다.', '표준 돌파장', '고전적 돌파 규칙과 잘 맞음', '진입이 늦거나 적을 수 있음', '적음~중간', ['UTBot', 'Donchian'], ['donchian_breakout'], {'donchian_length': 20}),
        (23, 'Breakout', 'UT + BB band breakout', '볼린저 상/하단 종가 돌파를 봅니다.', '변동성 돌파장', '돌파 해석이 쉬움', '상단 추격 리스크', '중간', ['UTBot', 'Bollinger Bands'], ['bb_band_breakout'], {}),
        (24, 'Breakout', 'UT + Keltner breakout', 'Keltner 채널 밖 종가 돌파를 봅니다.', 'ATR 기반 추세 돌파', '노이즈가 BB보다 적을 수 있음', '강한 변동성 후 늦음', '중간', ['UTBot', 'Keltner Channel'], ['keltner_breakout'], {}),
        (25, 'Breakout', 'UT + range expansion candle', '현재 봉 범위가 평균보다 커진 확장봉을 확인합니다.', '강한 캔들 돌파', '급가속 구간 포착', '꼬리 큰 봉에 취약', '중간', ['UTBot', 'Range Expansion'], ['range_expansion'], {}),
        (26, 'Pullback/Continuation', 'UT + VWAP pullback', 'VWAP 근처 눌림 후 UT 방향 지속을 봅니다.', '추세 중 눌림목', '추격보다 가격이 유리할 수 있음', '강한 추세에서는 못 탈 수 있음', '중간', ['UTBot', 'VWAP'], ['vwap_pullback'], {}),
        (27, 'Pullback/Continuation', 'UT + EMA pullback', 'EMA 근처 눌림 후 재개를 봅니다.', 'EMA를 따라가는 추세', '실전 해석이 쉬움', '횡보 EMA에서는 무의미', '중간', ['UTBot', 'EMA'], ['ema_pullback'], {}),
        (28, 'Pullback/Continuation', 'UT + BB midline reclaim', '볼린저 중심선 회복/이탈로 눌림 회복을 봅니다.', '중심선 리클레임 장', '추세 복귀 확인', '중심선 근처 노이즈', '중간', ['UTBot', 'Bollinger Midline'], ['bb_midline_reclaim'], {}),
        (29, 'Pullback/Continuation', 'UT + RSI pullback', 'RSI 눌림 후 방향 재개를 봅니다.', '모멘텀 유지 중 눌림', '과열 추격을 줄임', '추세 초반 진입 감소', '중간', ['UTBot', 'RSI'], ['rsi_pullback'], {}),
        (30, 'Pullback/Continuation', 'UT + ATR trailing continuation', 'ATR trailing 방향 유지로 추세 지속을 봅니다.', '추세가 길게 이어지는 장', '추세 유지 확인', '전환 초반 늦음', '적음~중간', ['UTBot', 'ATR Trail'], ['atr_trail_continuation'], {}),
        (31, 'Volume/Flow', 'UT + volume spike', '거래량 급증을 동반한 UT 방향만 봅니다.', '관심이 몰리는 돌파/추세', '약한 신호 제거', '저거래량 추세 놓침', '중간', ['UTBot', 'Volume'], ['volume_spike'], {}),
        (32, 'Volume/Flow', 'UT + relative volume', '상대 거래량이 평균보다 높은지 봅니다.', '평균보다 활발한 장', '실전적 유동성 필터', '거래량 없는 추세 제외', '중간', ['UTBot', 'Relative Volume'], ['relative_volume'], {}),
        (33, 'Volume/Flow', 'UT + OBV slope', 'OBV 기울기로 누적 매수/매도 흐름을 봅니다.', '수급 방향이 쌓이는 장', '가격보다 선행 가능성', '선물 거래량 해석 한계', '중간', ['UTBot', 'OBV'], ['obv_slope'], {}),
        (34, 'Volume/Flow', 'UT + MFI flow', 'Money Flow Index 방향을 확인합니다.', '가격과 거래량 흐름이 함께 움직일 때', '거래량 가중 모멘텀', '급변동 시 과열 신호', '중간', ['UTBot', 'MFI'], ['mfi_flow'], {}),
        (35, 'Volume/Flow', 'UT + VWAP slope', 'VWAP 기울기가 UT 방향과 맞는지 봅니다.', '당일 평균가격 흐름이 기울어진 장', '데이 트레이딩에 직관적', '세션 기준 변화에 민감', '중간', ['UTBot', 'VWAP'], ['vwap_slope'], {}),
        (36, 'Chop Avoidance', 'UT + CHOP avoid', 'CHOP 높은 구간을 회피합니다.', '횡보 회피 목적', 'false signal 감소', '진입 감소', '적음~중간', ['UTBot', 'CHOP'], ['chop_avoid'], {}),
        (37, 'Chop Avoidance', 'UT + Donchian width avoid', 'Donchian 폭이 너무 좁은 압축장을 피합니다.', '박스권 회피', '좁은 횡보 손실 감소', '압축 후 첫 돌파 놓침', '적음~중간', ['UTBot', 'Donchian Width'], ['donchian_width'], {}),
        (38, 'Chop Avoidance', 'UT + EMA gap avoid', 'EMA50/200 간격이 너무 좁으면 피합니다.', '추세 불명확 회피', '방향 없는 장 방어', '전환 초반 늦음', '적음', ['UTBot', 'EMA Gap'], ['htf_ema_gap'], {}),
        (39, 'Chop Avoidance', 'UT + BB squeeze avoid', '볼린저 squeeze 구간을 피합니다.', '압축 횡보 회피', '무의미한 신호 감소', 'squeeze breakout 초반 놓침', '적음', ['UTBot', 'BB Squeeze'], ['bb_squeeze_avoid'], {}),
        (40, 'Chop Avoidance', 'UT + range compression avoid', '최근 range 압축이 심하면 피합니다.', '좁은 박스권', '수수료 소모 감소', '초기 확장 누락 가능', '적음', ['UTBot', 'Range Compression'], ['range_compression_avoid'], {}),
        (41, 'MTF Alignment', 'UT + 15m/30m align', '15분과 30분 방향 일치를 봅니다.', '단기 MTF 정렬', '빠른 MTF 확인', '신호 감소', '중간', ['UTBot', '15m', '30m'], ['mtf_15_30'], {}),
        (42, 'MTF Alignment', 'UT + 30m/1h align', '30분과 1시간 방향 일치를 봅니다.', '중기 MTF 정렬', '노이즈 감소', '진입 늦음', '적음~중간', ['UTBot', '30m', '1h'], ['mtf_30_1h'], {}),
        (43, 'MTF Alignment', 'UT + 1h/4h align', '1시간과 4시간 방향 일치를 봅니다.', '큰 추세 정렬', '역추세 감소', '진입 매우 감소', '적음', ['UTBot', '1h', '4h'], ['mtf_1h_4h'], {}),
        (44, 'MTF Alignment', 'UT + MTF momentum score', '여러 봉 모멘텀 점수로 set을 고릅니다.', '모멘텀 점수 우위 장', '분산된 정보 활용', '구현 검증 필요', '중간', ['UTBot', 'MTF Momentum'], ['mtf_momentum_score'], {}),
        (45, 'MTF Alignment', 'UT + MTF volatility score', '여러 봉 변동성 점수로 set을 고릅니다.', '변동성 regime 전환', '장세 구분에 유리', '구현 검증 필요', '중간', ['UTBot', 'MTF Volatility'], ['mtf_volatility_score'], {}),
        (46, 'Special Regime', 'UT + Parabolic SAR', 'PSAR 방향과 UT 방향을 맞춥니다.', '추세 추종 특화', '명확한 trailing 구조', '횡보 whipsaw', '중간', ['UTBot', 'Parabolic SAR'], ['psar_direction'], {}),
        (47, 'Special Regime', 'UT + Ichimoku cloud', '일목 구름 위치로 큰 방향을 봅니다.', '중장기 방향성 장', '구조적 추세 판단', '계산/해석 복잡', '적음', ['UTBot', 'Ichimoku'], ['ichimoku_cloud'], {}),
        (48, 'Special Regime', 'UT + session/time volatility', '시간대별 변동성 특성을 반영합니다.', '세션별 움직임 차이가 큰 장', '실거래 시간대 최적화 가능', '시장 구조 변화에 민감', '중간', ['UTBot', 'Session'], ['session_volatility'], {}),
        (49, 'Special Regime', 'UT + market regime fallback', '분석 점수가 애매하면 안전한 단순 set으로 후퇴합니다.', '분류가 애매한 장', '과도한 필터링 방지', '방어력은 낮음', '많음', ['UTBot', 'Regime Score'], ['regime_fallback'], {}),
        (50, 'Special Regime', 'UT emergency simple mode', '장애/데이터 부족 시 UT와 리스크만 남기는 비상 단순 모드입니다.', '데이터 분석이 불안정할 때', '진입 로직이 멈추지 않음', '품질 필터 거의 없음', '매우 많음', ['UTBot', 'Risk Control'], [], {}),
        (51, 'Prediction Futures', 'UT + Orderflow Imbalance', 'Futures 호가 불균형과 taker buy/sell 흐름이 UT 방향과 맞을 때만 진입합니다.', '호가와 체결 흐름이 한쪽으로 기울어진 장', '체결 전 수급 압력을 반영', '얕은 호가/순간 노이즈에 취약', '적음~중간', ['UTBot', 'Futures Depth', 'Taker Buy/Sell'], ['prediction_orderflow_imbalance'], {}),
        (52, 'Prediction Futures', 'UT + OI/Funding Crowding', 'OI 변화와 funding/long-short crowding이 진입 방향에 과열되지 않았는지 확인합니다.', '군중 포지션이 한쪽으로 과도하게 몰린 장', 'crowded trade 회피', '보수적이라 일부 강한 추세를 놓칠 수 있음', '적음~중간', ['UTBot', 'Open Interest', 'Funding'], ['prediction_oi_funding_crowding'], {}),
        (53, 'Prediction Futures', 'UT + Liquidation Cascade Proxy', 'OI 급감과 taker 흐름, 확장봉으로 청산 연쇄 이후 방향 지속을 확인합니다.', '급격한 청산 연쇄 가능 구간', '가속 구간 포착 후보', '실제 청산맵이 아닌 public data proxy', '적음', ['UTBot', 'OI Delta', 'Taker Flow', 'Range Expansion'], ['prediction_liquidation_cascade'], {}),
        (54, 'Prediction Futures', 'UT + Prediction Odds Divergence', 'Prediction odds가 Futures 방향과 과도하게 충돌하지 않을 때만 허용합니다.', '예측시장 확률과 선물 방향을 같이 볼 때', '크로스마켓 정보 활용', 'Prediction 스캔 데이터가 없으면 진입 차단', '적음', ['UTBot', 'Prediction Odds'], ['prediction_odds_divergence'], {}),
        (55, 'Prediction Futures', 'UT + Event/Funding Guard', '중요 funding window와 과도한 funding rate 주변 진입을 회피합니다.', '이벤트/펀딩 직전 변동성 리스크', '불필요한 이벤트성 꼬리 위험 감소', '뉴스 캘린더 직접 연동은 아님', '적음~중간', ['UTBot', 'Funding Time', 'Funding Rate'], ['prediction_macro_event_guard'], {}),
        (56, 'Prediction Futures', 'UT + Volatility Forecast', 'ATR/BB/Keltner 기반 변동성 예측 점수가 정상일 때만 진입합니다.', '변동성 regime 전환 전후', '손익비 악화 구간 회피', '강한 고변동 추세 일부 제외', '적음~중간', ['UTBot', 'ATR', 'BB Width', 'Keltner'], ['prediction_volatility_forecast'], {}),
        (57, 'Prediction Futures', 'UT + Spread Depth Guard', 'Futures 스프레드와 호가 깊이가 실거래 비용을 감당할 수준인지 확인합니다.', '호가 얕은 변동장', '실제 체결 비용 방어', '저유동 알트 진입 감소', '적음~중간', ['UTBot', 'Spread', 'Depth'], ['prediction_spread_depth_guard'], {}),
        (58, 'Prediction Futures', 'UT + Basis Divergence', 'Mark/Index basis가 진입 방향으로 과열되지 않았는지 확인합니다.', 'basis 왜곡 장', '파생시장 과열 감지', 'basis 해석이 심볼별로 다를 수 있음', '적음~중간', ['UTBot', 'Basis', 'Mark/Index'], ['prediction_basis_divergence'], {}),
        (59, 'Prediction Futures', 'UT + Probability Trail Guard', 'Prediction 확률이 진입 방향을 일정 수준 이상 지지할 때만 허용합니다.', 'Prediction 확률 우위가 뚜렷한 장', '진입 후 확률 약화 감시 후보', 'Prediction 데이터 없으면 진입 차단', '적음', ['UTBot', 'Prediction Probability'], ['prediction_probability_trail'], {}),
        (60, 'Prediction Futures', 'UT + Cross-Market Confirmation', 'Futures 수급, basis/funding, Prediction odds 중 2개 이상이 UT 방향과 맞을 때 진입합니다.', '크로스마켓 정렬 장', '품질 좋은 신호 선별', '신호 수 감소', '적음', ['UTBot', 'Futures Flow', 'Basis', 'Prediction'], ['prediction_cross_market_confirmation'], {}),
        (61, 'Microstructure Futures', 'UT + Rolling OFI Confirmation', 'UTBot 방향 신호에 최근 orderbook imbalance, taker flow, spread/depth 비용을 같이 확인하는 futures microstructure set입니다.', '짧은 호가 수급이 UT 방향으로 누적되는 구간', '순간 호가가 아니라 rolling pressure를 확인', '호가 snapshot 품질과 호출 주기에 민감', '적음~중간', ['UTBot', 'Rolling OFI', 'Orderbook Imbalance', 'Taker Buy/Sell Ratio', 'Spread/Depth'], ['rolling_orderflow_imbalance', 'prediction_spread_depth_guard'], {'rolling_ofi_window': 5, 'rolling_ofi_min_abs': 3.0, 'rolling_ofi_taker_long_min': 1.03, 'rolling_ofi_taker_short_max': 0.97, 'rolling_ofi_spread_max_pct': 0.05, 'prediction_min_depth_usdt': 50000.0}),
        (62, 'Prediction Futures', 'UT + OI Funding Squeeze', 'UTBot 방향 신호에 OI 변화 z-score, funding 과열, long/short crowding, basis를 조합하는 set입니다.', 'OI가 가속되지만 funding/crowding이 아직 과열되지 않은 구간', '추세 가속과 군중 과열을 같이 점검', 'OI 히스토리 데이터 부족 시 보수적으로 대기', '적음', ['UTBot', 'Open Interest Z', 'Funding Rate', 'Long/Short Ratio', 'Basis'], ['oi_funding_squeeze', 'prediction_macro_event_guard', 'prediction_basis_divergence', 'prediction_spread_depth_guard'], {'oi_z_min': 0.75, 'oi_acceleration_min': 0.0, 'funding_long_max': 0.0008, 'funding_short_min': -0.0008, 'long_short_long_max': 1.85, 'long_short_short_min': 0.55}),
        (63, 'Volatility Regime', 'UT + Squeeze Release Breakout', 'BB/Keltner 압축 이후 range breakout이 발생하고 UTBot 방향과 일치할 때만 진입하는 set입니다.', '압축 뒤 변동성 확장이 시작되는 구간', '압축 후 돌파만 선택해 추격 노이즈를 줄임', '초기 압축 판정 데이터가 부족하면 대기', '적음~중간', ['UTBot', 'BB Width Percentile', 'Keltner Channel', 'Range Expansion', 'Relative Volume', 'ATR'], ['squeeze_release_breakout', 'atr_guard'], {'squeeze_lookback': 80, 'squeeze_percentile_max': 25.0, 'squeeze_release_range_min': 1.05, 'squeeze_volume_ratio_min': 1.20, 'squeeze_require_keltner': False}),
        (64, 'EV Adaptive', 'UT + Cost-Aware Regime Router', '기존 단일지표 Set 대신 추세/압축돌파/NO_TRADE를 비용 차감 기대값으로 분류합니다.', '추세와 압축돌파를 구분하고 횡보·패닉반등을 회피하는 장', '중복 필터와 Set 과최적화를 줄이고 실제 거래비용을 반영', '충분한 실거래 표본 전까지 보수적 승격 필요', '중간', ['UTBot', 'Trend Regime', 'Squeeze Release', 'Net EV', 'Liquidity'], [], {'ev_adaptive_enabled': True}),
    ]
    registry = {}
    for row in rows:
        set_id, family, name, description, regime, pros, cons, frequency, indicators, filters, params = row
        merged_params = dict(base_params)
        merged_params.update(params or {})
        registry[int(set_id)] = _build_utbreakout_set(
            set_id,
            family,
            name,
            description,
            regime,
            pros,
            cons,
            frequency,
            indicators,
            filters,
            merged_params,
        )
    return registry


UTBREAKOUT_SET_REGISTRY = build_utbreakout_set_registry()


def normalize_utbreakout_set_id(value, default=UTBREAKOUT_DEFAULT_SET_ID):
    try:
        text = str(value or '').strip().lower()
        if text.startswith('set'):
            text = text[3:]
        set_id = int(float(text))
    except (TypeError, ValueError):
        set_id = int(default)
    if set_id not in UTBREAKOUT_SET_REGISTRY:
        set_id = int(default)
    return set_id


def get_utbreakout_set_definition(set_id):
    return UTBREAKOUT_SET_REGISTRY.get(
        normalize_utbreakout_set_id(set_id),
        UTBREAKOUT_SET_REGISTRY[UTBREAKOUT_DEFAULT_SET_ID],
    )


def _parse_bool_like(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'y', 'on', 'enable', 'enabled'}:
        return True
    if text in {'0', 'false', 'no', 'n', 'off', 'disable', 'disabled'}:
        return False
    return bool(default)


def is_utbreakout_live_mode_value(value):
    text = str(value or '').strip().lower()
    return text in {BINANCE_MAINNET, 'mainnet', 'live', 'real', 'production', 'prod'}


def normalize_utbreakout_set_id_list(value, default=None):
    """Normalize comma/list style Set IDs into active UTBreakout set IDs."""
    default_values = list(default or [])
    if value is None:
        raw_items = default_values
    elif isinstance(value, str):
        raw_items = [item.strip() for item in value.split(',') if item.strip()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = list(value)
    else:
        raw_items = default_values

    result = []
    for item in raw_items:
        try:
            set_id = normalize_utbreakout_set_id(item, None)
        except Exception:
            continue
        if set_id in UTBREAKOUT_SET_REGISTRY and set_id <= UTBREAKOUT_ACTIVE_SET_MAX:
            if set_id not in result:
                result.append(int(set_id))

    if not result:
        result = [int(v) for v in default_values if int(v) in UTBREAKOUT_SET_REGISTRY]
    return result


def build_utbreakout_effective_config_diff_text(raw_cfg, effective_cfg):
    """Format raw/effective UTBreakout config differences for Telegram diagnostics."""
    raw_cfg = dict(raw_cfg or {})
    effective_cfg = dict(effective_cfg or {})

    watch_keys = [
        'effective_profile_version',
        'runtime_profile',
        'ev_adaptive_enabled',
        'selection_mode',
        'auto_select_enabled',
        'active_set_id',
        'safe_live_default_set_id',
        'live_safety_guard_enabled',
        'live_auto_set_whitelist_enabled',
        'live_auto_set_whitelist',
        'auto_min_score_margin_live',
        'auto_min_adjusted_score_live',
        'auto_block_on_weak_margin_live',
        'auto_multiple_testing_penalty_enabled',
        'set_selection_hysteresis_enabled',
        'set_selection_switch_margin',
        'set_selection_min_score_gap',
        'set_complexity_penalty_enabled',
        'selected_set_core_filter_hard_block_enabled',
        'set32_min_relative_volume',
        'set32_require_direction_candle',
        'set32_require_ema50_side',
        'set32_require_orderflow_confirmation',
        'set32_orderflow_min_samples',
        'market_quality_long_hard_block_on_multi_adverse_enabled',
        'market_quality_long_multi_adverse_min_reasons',
        'market_quality_long_multi_adverse_max_multiplier',
        'entry_quality_gate_enabled',
        'entry_quality_gate_min_final_risk_multiplier',
        'entry_quality_gate_long_min_final_risk_multiplier',
        'entry_quality_gate_short_min_final_risk_multiplier',
        'entry_quality_gate_min_ev_score',
        'entry_quality_gate_min_ev_probability',
        'entry_quality_gate_min_ev_net_expectancy_r',
        'entry_quality_gate_min_ev_mtf_votes',
        'entry_edge_enabled',
        'entry_edge_min_score',
        'entry_edge_long_min_score',
        'entry_edge_short_min_score',
        'entry_edge_min_probability',
        'entry_edge_long_min_probability',
        'entry_edge_short_min_probability',
        'entry_edge_min_net_expectancy_r',
        'direction_engine_min_score',
        'direction_engine_opposite_regime_min_score',
        'entry_type_max_chase_extension_atr',
        'entry_type_pullback_extension_atr',
        'entry_type_breakout_min_range',
        'entry_type_sweep_wick_ratio',
        'exit_meta_min_samples',
        'exit_meta_expectancy_reduce_below',
        'ev_min_entry_score',
        'ev_min_net_expectancy_r',
        'ev_no_edge_relief_enabled',
        'ev_no_edge_relief_min_score',
        'ev_no_edge_relief_min_adx',
        'ev_no_edge_relief_min_volume_ratio',
        'ev_no_edge_relief_min_efficiency',
        'ev_no_edge_relief_min_range_expansion',
        'ev_short_min_entry_score',
        'ev_short_trend_min_adx',
        'ev_short_trend_min_volume_ratio',
        'ev_short_no_edge_relief_min_score',
        'ev_short_no_edge_relief_min_adx',
        'ev_short_no_edge_relief_min_volume_ratio',
        'ev_short_no_edge_relief_min_efficiency',
        'ev_short_no_edge_relief_min_range_expansion',
        'ev_short_conditional_relief_risk_cap',
        'ev_short_relaxed_signal_risk_cap',
        'trend_continuation_entry_enabled',
        'trend_continuation_base_risk_multiplier',
        'trend_continuation_min_risk_multiplier',
        'entry_timeframe',
        'exit_timeframe',
        'htf_timeframe',
        'partial_take_profit_r_multiple',
        'partial_take_profit_ratio',
        'second_take_profit_r_multiple',
        'second_take_profit_ratio',
        'structure_stop_lookback_bars',
        'structure_stop_buffer_atr',
        'take_profit_front_run_atr',
        'take_profit_front_run_pct',
        'soft_stop_enabled',
        'soft_stop_confirm_bars',
        'near_miss_tp_enabled',
        'near_miss_tp_arm_ratio',
        'near_miss_tp_lock_r',
        'near_miss_tp_rejection_atr',
        'profit_alpha_stall_exit_max_current_r',
        'ev_time_stop_max_current_r',
        'time_stop_max_current_r',
        'market_regime_engine_enabled',
        'data_quality_engine_enabled',
        'execution_quality_engine_enabled',
        'protection_health_engine_enabled',
        'protection_health_execution_gate_enabled',
        'signal_attribution_engine_enabled',
        'strategy_replay_engine_enabled',
        'overfit_governance_enabled',
        'overfit_governance_hard_block_enabled',
        'overfit_min_samples',
        'overfit_max_pbo',
        'atr_trailing_activation_r',
        'atr_trailing_multiplier',
    ]

    lines = [
        '🧾 UT Breakout Config Diff',
        '',
        '[Effective 핵심값]',
    ]

    for key in watch_keys:
        if key in effective_cfg:
            lines.append(f"- {key}: {effective_cfg.get(key)!r}")

    changed = []
    for key in sorted(set(raw_cfg.keys()) | set(effective_cfg.keys())):
        raw_value = raw_cfg.get(key, '<missing>')
        eff_value = effective_cfg.get(key, '<missing>')
        if raw_value != eff_value:
            changed.append((key, raw_value, eff_value))

    lines.extend([
        '',
        '[Raw -> Effective 변경값]',
    ])

    if not changed:
        lines.append('- 변경 없음')
    else:
        for key, raw_value, eff_value in changed[:120]:
            lines.append(f"- {key}: {raw_value!r} -> {eff_value!r}")
        if len(changed) > 120:
            lines.append(f"- ... and {len(changed) - 120} more")

    return '\n'.join(lines)


def validate_utbreakout_runtime_set_id(cfg, set_id, is_live=False):
    cfg = cfg if isinstance(cfg, dict) else {}
    selected_id = normalize_utbreakout_set_id(set_id, UTBREAKOUT_DEFAULT_SET_ID)
    if not _parse_bool_like(cfg.get('live_safety_guard_enabled', True), True):
        return selected_id, None

    safe_id = normalize_utbreakout_set_id(
        cfg.get('safe_live_default_set_id', UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID),
        UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID
    )
    if safe_id in {1, 50}:
        safe_id = UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID

    inferred_live = (
        bool(is_live)
        or _parse_bool_like(cfg.get('live_trading'), False)
        or is_utbreakout_live_mode_value(cfg.get('mode'))
        or is_utbreakout_live_mode_value(cfg.get('exchange_mode'))
    )

    auto_mode = (
        _parse_bool_like(cfg.get('auto_select_enabled', False), False)
        or str(cfg.get('selection_mode') or '').strip().lower() == 'auto'
    )

    if inferred_live and auto_mode and _parse_bool_like(cfg.get('live_auto_set_whitelist_enabled', True), True):
        whitelist = normalize_utbreakout_set_id_list(
            cfg.get('live_auto_set_whitelist'),
            UTBREAKOUT_LIVE_AUTO_SET_WHITELIST,
        )
        if selected_id not in whitelist:
            return safe_id, (
                f"Set{selected_id} live AUTO whitelist blocked "
                f"(allowed {','.join('Set' + str(x) for x in whitelist)}) "
                f"-> Set{safe_id} safety fallback"
            )

    if selected_id == 1 and inferred_live and not _parse_bool_like(cfg.get('allow_ut_only_live_override', False), False):
        return safe_id, f"Set1 UT-only live blocked -> Set{safe_id} safety fallback"
    if selected_id == 50 and not (
        _parse_bool_like(cfg.get('emergency_mode', False), False)
        or _parse_bool_like(cfg.get('allow_emergency_simple_set', False), False)
    ):
        return safe_id, f"Set50 emergency simple mode blocked outside emergency -> Set{safe_id} safety fallback"
    return selected_id, None


def format_utbreakout_set_brief(set_id):
    info = get_utbreakout_set_definition(set_id)
    status = '실거래' if info.get('status') == 'active' else '연구전용'
    return (
        f"Set{info['id']} {info['name']} [{status}] - {info['description']} "
        f"진입빈도: {info['frequency_impact']}"
    )


def build_default_utbot_filter_pack():
    return {
        'entry': {
            'selected': [],
            'logic': 'and'
        },
        'exit': {
            'selected': [],
            'logic': 'and',
            'mode_by_filter': {}
        }
    }


def build_default_utbot_filtered_breakout_config():
    return {
        'profile': 'set2',
        'active_set_id': UTBREAKOUT_DEFAULT_SET_ID,
        'safe_live_default_set_id': UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID,
        'live_safety_guard_enabled': True,
        'allow_ut_only_live_override': False,
        'emergency_mode': False,
        'allow_emergency_simple_set': False,
        'auto_select_enabled': False,
        'selection_mode': 'manual',

        # Live AUTO overfit guards
        'live_auto_set_whitelist_enabled': True,
        'live_auto_set_whitelist': list(sorted(UTBREAKOUT_LIVE_AUTO_SET_WHITELIST)),
        'auto_min_score_margin_live': 0.5,
        'auto_min_adjusted_score_live': 40.0,
        'auto_block_on_weak_margin_live': True,
        'auto_multiple_testing_penalty_enabled': True,
        'multiple_testing_free_trials': 25,
        'multiple_testing_max_score_penalty': 4.0,
        'set_selection_hysteresis_enabled': True,
        'set_selection_switch_margin': 3.0,
        'set_selection_min_score_gap': 2.0,
        'set_complexity_penalty_enabled': True,

        'auto_timeframes': list(UTBREAKOUT_AUTO_TIMEFRAMES),
        'adaptive_timeframe_enabled': False,
        'adaptive_timeframes': list(UTBREAKOUT_AUTO_TIMEFRAMES),
        'adaptive_timeframe_min_score': 45.0,
        'adaptive_timeframe_switch_margin': 8.0,
        'adaptive_timeframe_min_hold_candles': 3,
        'entry_timeframe': '15m',
        'exit_timeframe': '15m',
        'htf_timeframe': '1h',
        'dual_alpha_direction_filter_enabled': False,
        'dual_alpha_direction_filter_timeframe': '4h',
        'dual_alpha_direction_filter_htf': '1d',
        'dual_alpha_single_signal_risk_multiplier': 0.60,
        'triple_alpha_three_signal_risk_multiplier': 1.00,
        'triple_alpha_two_signal_risk_multiplier': 0.85,
        'triple_alpha_single_signal_risk_multiplier': 0.55,
        'quad_alpha_four_signal_risk_multiplier': 1.00,
        'quad_alpha_five_signal_risk_multiplier': 1.00,
        'quad_alpha_three_signal_risk_multiplier': 0.90,
        'quad_alpha_two_signal_risk_multiplier': 0.75,
        'quad_alpha_single_signal_risk_multiplier': 0.45,
        'quad_alpha_enabled_strategies': list(QUAD_ALPHA_BRANCH_ORDER),
        'qh_flow': default_qh_flow_config(),
        'qh_flow_live_enabled': False,
        'qh_flow_confirmation_enabled': True,
        'l2_gate_enabled': True,
        'crowding_unwind': default_crowding_unwind_config(),
        'crowding_unwind_live_enabled': False,
        'liquidation_exhaustion_reversal': default_liquidation_exhaustion_reversal_config(),
        'liquidation_exhaustion_reversal_live_enabled': False,
        'lxr_migration_v1_complete': False,
        'strategy_allocator': default_strategy_allocator_config(),
        'strategy_allocator_enabled': True,
        'entry_strategy': ENTRY_STRATEGY_UT_BREAKOUT,
        'relative_strength_pullback_trend': default_relative_strength_pullback_config(),
        'relative_strength_pullback_trend_shadow_enabled': True,
        'relative_strength_pullback_trend_live_enabled': False,
        'relative_strength_pullback_trend_paper_enabled': False,
        'relative_strength_pullback_trend_key': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        'utbot_key_value': 2.5,
        'utbot_atr_period': 14,
        'legacy_utbot_key_value': 2.0,
        'legacy_utbot_atr_period': 10,
        'use_heikin_ashi': False,
        'ema_fast': 50,
        'ema_slow': 200,
        'rsi_length': 14,
        'rsi_threshold': 50.0,
        'rsi_long_extreme': 80.0,
        'rsi_short_extreme': 20.0,
        'exclude_rsi_extreme': False,
        'adx_length': 14,
        'adx_threshold': 22.0,
        'atr_length': 14,
        'atr_min_percent': 0.12,
        'atr_max_percent': 1.20,
        'donchian_length': 20,
        'ema_near_percent': 0.20,
        'htf_ema_gap_min_percent': 0.15,
        'donchian_width_min_percent': 0.50,
        'stop_atr_multiplier': 1.5,
        'take_profit_r_multiple': 3.50,
        'fixed_take_profit_enabled': True,
        'partial_take_profit_enabled': True,
        'partial_take_profit_r_multiple': 1.00,
        'partial_take_profit_ratio': 0.20,
        'second_take_profit_enabled': True,
        'second_take_profit_r_multiple': 3.50,
        'second_take_profit_ratio': 0.40,
        'enable_tp2_fallback_close': False,
        'tp2_fallback_confirm_loops': 2,
        'tp2_fallback_use_market': True,
        'atr_trailing_enabled': True,
        'atr_trailing_multiplier': 3.50,
        'atr_trailing_activation_r': 1.60,
        'atr_trailing_breakeven_enabled': True,
        'short_conservative_enabled': True,
        'short_risk_multiplier': 0.5,
        'short_adx_threshold': 25.0,
        'short_dmi_min_gap': 4.0,
        'short_require_htf_supertrend': True,
        'short_require_entry_ema_downtrend': True,
        'short_require_momentum_downtrend': True,
        'bias_continuation_enabled': True,
        'bias_continuation_risk_multiplier': 0.65,
        'bias_continuation_15m_risk_multiplier': 0.50,
        'bias_continuation_max_signal_age_candles': 10,
        'bias_continuation_15m_max_signal_age_candles': 10,
        'bias_continuation_min_adx': 10.0,
        'bias_continuation_15m_min_adx': 11.0,
        'bias_continuation_min_volume_ratio': 0.40,
        'bias_continuation_15m_min_volume_ratio': 0.45,
        'bias_continuation_max_extension_atr': 1.60,
        'bias_continuation_15m_max_extension_atr': 1.50,
        'bias_continuation_min_adaptive_tf_score': 30.0,
        'bias_continuation_15m_min_adaptive_tf_score': 32.0,
        'utbreakout_no_position_retry_interval_sec': 20.0,
        'utbreakout_trace_watchdog_wait_sec': 90.0,
        'utbreakout_trace_watchdog_cooldown_sec': 180.0,
        'utbreakout_auto_entry_bridge_cooldown_sec': 60.0,
        'utbreakout_auto_entry_bridge_max_ready_age_sec': 180.0,
        'utbreakout_require_scanner_candidate_for_auto_entry': True,
        'quality_score_v2_enabled': True,
        'quality_score_v2_block_below': 12.0,
        'quality_score_v2_reduce_below': 40.0,
        'quality_score_v2_full_score': 82.0,
        'quality_score_v2_min_risk_multiplier': 0.60,
        'quality_score_v2_15m_block_below': 12.0,
        'quality_score_v2_15m_reduce_below': 40.0,
        'quality_score_v2_long_block_below': 12.0,
        'quality_score_v2_long_reduce_below': 40.0,
        'quality_score_v2_long_15m_block_below': 12.0,
        'quality_score_v2_long_15m_reduce_below': 40.0,
        'quality_score_v2_short_block_below': 16.0,
        'quality_score_v2_short_reduce_below': 45.0,
        'quality_score_v2_short_15m_block_below': 16.0,
        'quality_score_v2_short_15m_reduce_below': 45.0,
        'dynamic_take_profit_enabled': True,
        'dynamic_tp2_strong_score': 72.0,
        'dynamic_tp2_elite_score': 82.0,
        'dynamic_tp2_base_r_multiple': 3.20,
        'dynamic_tp2_strong_r_multiple': 5.00,
        'dynamic_tp2_elite_r_multiple': 7.00,
        'tp1_breakeven_enabled': True,
        'tp1_breakeven_trigger_r': 1.5,
        'tp1_breakeven_offset_r': 0.03,
        'tp1_breakeven_wait_for_partial': True,
        'tp1_breakeven_qty_tolerance': 0.08,
        'market_quality_enabled': True,
        'regime_filter_enabled': False,
        'meta_sizing_enabled': False,
        'portfolio_risk_enabled': False,
        'drawdown_brake_enabled': False,
        'adaptive_exit_v2_enabled': False,
        'walk_forward_report_enabled': False,
        'utbreak_entry_relaxation_mode': 'balanced',
        'entry_relaxation_balanced_score_buffer': 1.5,
        'entry_relaxation_active_score_buffer': 3.0,
        'entry_relaxation_balanced_probability_buffer': 0.005,
        'entry_relaxation_active_probability_buffer': 0.010,
        'market_quality_data_required': False,
        'market_quality_min_risk_multiplier': 0.55,
        'market_quality_long_hard_block_on_multi_adverse_enabled': False,
        'market_quality_long_multi_adverse_min_reasons': 5,
        'market_quality_long_multi_adverse_max_multiplier': 0.35,
        'entry_quality_gate_enabled': True,
        'entry_quality_gate_min_final_risk_multiplier': 0.35,
        'entry_quality_gate_long_min_final_risk_multiplier': 0.40,
        'entry_quality_gate_short_min_final_risk_multiplier': 0.35,
        'entry_quality_gate_hard_market_multiplier_below': 0.25,
        'entry_quality_gate_min_ev_score': 58.0,
        'entry_quality_gate_min_ev_probability': 0.53,
        'entry_quality_gate_min_ev_net_expectancy_r': 0.18,
        'entry_quality_gate_min_ev_mtf_votes': 2,
        'market_quality_high_atr_pct': 1.5,
        'market_quality_extreme_atr_pct': 2.5,
        'market_quality_adverse_funding_soft': 0.0006,
        'market_quality_adverse_funding_hard': 0.0015,
        'market_quality_regime_enabled': True,
        'market_quality_regime_symbols': ['BTC/USDT', 'ETH/USDT'],
        'market_quality_regime_timeframe': '4h',
        'market_quality_regime_strong_move_pct': 1.5,
        'shadow_triple_barrier_enabled': True,
        'shadow_triple_barrier_max_bars': 24,
        'shadow_runner_exit_enabled': True,
        'shadow_runner_max_bars': 48,
        'runner_exit_enabled': True,
        'runner_chandelier_enabled': True,
        'runner_chandelier_lookback': 22,
        'runner_structure_lookback': 5,
        'runner_structure_buffer_atr': 0.20,
        'runner_dynamic_multiplier_enabled': True,
        'runner_chandelier_multiplier': 3.80,
        'runner_chandelier_multiplier_min': 1.4,
        'runner_chandelier_multiplier_max': 4.50,
        'runner_mfe_tighten_r': 3.0,
        'runner_mfe_tighten_delta': 0.20,
        'trend_health_enabled': True,
        'trend_health_directional_lookback': 12,
        'trend_health_volatility_long_length': 50,
        'trend_health_hard_block_below': 12.0,
        'trend_health_reduce_below': 35.0,
        'trend_health_full_score': 62.0,
        'trend_health_min_multiplier': 0.55,
        'strategy_quality_hard_block_below': 8.0,
        'strategy_quality_reduce_below': 25.0,
        'strategy_quality_full_score': 60.0,
        'strategy_quality_min_multiplier': 0.55,
        'adaptive_exit_enabled': True,
        'adaptive_exit_min_samples': 8,
        'adaptive_exit_lookback_days': 14,
        'adaptive_exit_partial_r_min': 1.0,
        'adaptive_exit_partial_r_max': 1.2,
        'adaptive_exit_ratio_min': 0.20,
        'adaptive_exit_ratio_max': 0.35,
        'adaptive_exit_trailing_multiplier_min': 3.0,
        'adaptive_exit_trailing_multiplier_max': 4.0,
        'adaptive_exit_activation_r_min': 1.4,
        'adaptive_exit_activation_r_max': 1.8,
        'volatility_targeting_enabled': True,
        'volatility_target_atr_pct': 1.0,
        'volatility_target_min_multiplier': 0.25,
        'meta_labeling_enabled': True,
        'meta_labeling_min_samples': 12,
        'meta_labeling_min_multiplier': 0.5,
        'strategy_adaptive_min_risk_multiplier': 0.50,
        'selected_set_core_filter_hard_block_enabled': True,
        'set_filter_soft_fail_enabled': True,
        'set_filter_soft_fail_multiplier': 0.90,
        'set_filter_multi_soft_fail_multiplier': 0.80,
        'final_risk_multiplier_floor': 0.20,

        # Set32 live strengthening
        'set32_min_relative_volume': 1.15,
        'set32_require_direction_candle': True,
        'set32_require_ema50_side': True,
        'set32_require_orderflow_confirmation': True,
        'set32_orderflow_min_samples': 2,
        'set32_min_taker_ratio_long': 0.97,
        'set32_max_taker_ratio_short': 1.03,
        'set32_max_spread_pct': 0.09,
        'short_asymmetry_enabled': True,
        'short_partial_take_profit_r_delta': 0.20,
        'short_partial_take_profit_ratio_add': 0.10,
        'short_atr_trailing_multiplier_delta': 0.25,
        'short_atr_trailing_activation_r_delta': 0.20,
        'min_risk_reward': 2.0,
        'risk_per_trade_percent': DEFAULT_RISK_PER_TRADE_PERCENT,
        'min_risk_per_trade_percent': DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
        'max_risk_per_trade_percent': UTBREAKOUT_MAX_RISK_PER_TRADE_PERCENT,
        'risk_budget_tracks_account_equity': True,
        'max_risk_per_trade_usdt_hard_cap': 0.0,
        'sl_place_max_retries': 3,
        'sl_retry_delay_sec': 0.7,
        'emergency_close_on_sl_fail': True,
        'protection_audit_after_place_delay_sec': 0.5,
        'aggressive_growth_enabled': False,
        'aggressive_growth_balance_sleeve_pct': 0.20,
        'aggressive_growth_sleeve_mode': 'notional',
        'aggressive_growth_max_leverage_for_margin_sleeve': 3.0,
        'aggressive_growth_max_trade_risk_pct': 0.015,
        'aggressive_growth_max_trade_risk_pct_strong': 0.025,
        'aggressive_growth_daily_loss_limit_pct': 0.04,
        'aggressive_growth_weekly_loss_limit_pct': 0.10,
        'aggressive_growth_max_open_positions': 2,
        'aggressive_growth_max_symbol_exposure_pct': 0.10,
        'aggressive_growth_pyramiding_enabled': True,
        'aggressive_growth_pyramid_trigger_r': 1.0,
        'aggressive_growth_pyramid_max_adds': 2,
        'aggressive_growth_pyramid_add_risk_fraction': 0.50,
        'aggressive_growth_move_sl_to_breakeven_before_add': True,
        'aggressive_growth_trailing_atr_multiplier': 2.5,
        'aggressive_growth_runner_pct': 0.35,
        'aggressive_growth_tp1_pct': 0.35,
        'aggressive_growth_tp2_pct': 0.30,
        'aggressive_growth_vol_target_enabled': True,
        'aggressive_growth_atr_pct_low': 0.004,
        'aggressive_growth_atr_pct_normal': 0.012,
        'aggressive_growth_atr_pct_high': 0.025,
        'aggressive_growth_atr_pct_extreme': 0.040,
        'aggressive_growth_kelly_enabled': False,
        'aggressive_growth_kelly_lookback_trades': 50,
        'aggressive_growth_kelly_fraction': 0.25,
        'aggressive_growth_kelly_max_risk_multiplier': 1.25,
        'aggressive_growth_kelly_min_risk_multiplier': 0.25,
        'aggressive_growth_cppi_enabled': False,
        'aggressive_growth_cppi_floor_pct': 0.90,
        'aggressive_growth_cppi_multiplier': 2.0,
        'aggressive_growth_cppi_min_sleeve_pct': 0.05,
        'aggressive_growth_cppi_max_sleeve_pct': 0.20,
        'max_risk_per_trade_usdt': 1.0,
        'daily_max_loss_usdt': 3.0,
        'max_daily_trades': 5,
        'max_consecutive_losses': 5,
        'daily_profit_target_enabled': False,
        'daily_profit_target_usdt': 5.0,
        'opposite_signal_exit_enabled': False,
        'opposite_set_exit_enabled': False,
        'opposite_set_exit_min_hold_candles': 3,
        'opposite_set_exit_min_pnl_enabled': False,
        'opposite_set_exit_min_pnl_usdt': 0.0,
        'ema_rsi_exit_enabled': False,
        'adx_donchian_exit_enabled': False
    }


def build_utbot_filtered_breakout_profile(profile):
    profile_key = str(profile or 'set2').strip().lower()
    set_id = normalize_utbreakout_set_id(profile_key, UTBREAKOUT_DEFAULT_SET_ID)
    cfg = build_default_utbot_filtered_breakout_config()
    if profile_key in {'aggressive'}:
        set_id = 1
    elif profile_key in {'conservative'}:
        set_id = 7
    set_info = get_utbreakout_set_definition(set_id)
    cfg.update(set_info.get('params', {}))
    cfg['profile'] = f"set{set_id}"
    cfg['active_set_id'] = set_id
    cfg['selection_mode'] = 'manual'
    cfg['auto_select_enabled'] = False
    return cfg

__all__ = (
    'BINANCE_FAPI_PUBLIC_BASE_URL',
    'UTBREAKOUT_ACTIVE_SET_MAX',
    'UTBREAKOUT_DEFAULT_SET_ID',
    'UTBREAKOUT_SAFE_LIVE_DEFAULT_SET_ID',
    'UTBREAKOUT_AUTO_TIMEFRAMES',
    'UTBREAKOUT_RUNTIME_PROFILE',
    'UTBREAKOUT_EFFECTIVE_PROFILE_VERSION',
    'UTBREAKOUT_LIVE_AUTO_SET_WHITELIST',
    'UTBREAKOUT_CORE_SET_FILTER_HARD_NAMES',
    'UTBREAKOUT_CORE_SET_FILTER_HARD_CODES',
    '_safe_set_id',
    '_estimate_set_complexity_penalty',
    '_rank_utbreakout_sets_stably',
    '_first_present_metric',
    '_classify_set_filter_result',
    '_apply_utbreakout_risk_multiplier_floor',
    '_utbreakout_stale_signal_multiplier',
    'apply_profit_opportunity_effective_overrides',
    '_ev_adaptive_runtime_config',
    '_apply_ev_exit_profile',
    'build_utbreakout_effective_status_contract',
    'enforce_utbreakout_effective_status_contract',
    'apply_stable_utbreak_final_overrides',
    '_build_utbreakout_set',
    'build_utbreakout_set_registry',
    'UTBREAKOUT_SET_REGISTRY',
    'normalize_utbreakout_set_id',
    'get_utbreakout_set_definition',
    '_parse_bool_like',
    'is_utbreakout_live_mode_value',
    'normalize_utbreakout_set_id_list',
    'build_utbreakout_effective_config_diff_text',
    'validate_utbreakout_runtime_set_id',
    'format_utbreakout_set_brief',
    'build_default_utbot_filter_pack',
    'build_default_utbot_filtered_breakout_config',
    'build_utbot_filtered_breakout_profile',
)

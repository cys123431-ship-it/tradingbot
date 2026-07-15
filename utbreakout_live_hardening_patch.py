"""Runtime UTBreakout live hardening + profit opportunity patch.

Loaded from scripts/launch_emas.py before emas.py is executed.  It avoids
replacing the very large emas.py file while enforcing the selected Set core
hard-block guard and applying a more profit-oriented opportunity profile.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

log = logging.getLogger("utbreak_live_hardening")

CORE_SET_FILTER_HARD_NAMES = {
    "Donchian 돌파",
    "BB 밴드 돌파",
    "Keltner 돌파",
    "Range 확장봉",
    "거래량 급증",
    "상대 거래량",
    "Rolling OFI 확인",
    "OI/Funding Squeeze",
    "Squeeze Release 돌파",
    "Futures 수급 불균형",
    "OI/Funding 과열회피",
}

CORE_SET_FILTER_HARD_CODES = {
    "REJECTED_DONCHIAN_NO_BREAKOUT",
    "REJECTED_RELATIVE_VOLUME_CORE",
    "REJECTED_ROLLING_OFI",
    "REJECTED_OI_FUNDING_SQUEEZE",
    "REJECTED_SQUEEZE_RELEASE",
    "REJECTED_PREDICTION_FLOW",
    "REJECTED_PREDICTION_CROWDING",
}

# Runtime mirror of the single live EV Adaptive router in emas.py.  This module
# loads before emas.py, so it must not restore the retired many-Set profile.
PROFIT_MAX_OVERRIDES = {
    "effective_profile_version": "ev_adaptive_v3_profit_engine",
    "ev_adaptive_enabled": True,
    "legacy_sets_research_only": True,

    "selection_mode": "auto",
    "auto_select_enabled": True,
    "live_auto_set_whitelist_enabled": True,
    "live_auto_set_whitelist": [64],
    "active_set_id": 64,
    "profile": "set64",
    "safe_live_default_set_id": 64,
    "auto_block_on_weak_margin_live": False,
    "auto_multiple_testing_penalty_enabled": False,

    "selected_set_core_filter_hard_block_enabled": False,
    "set_filter_soft_fail_enabled": False,

    # Set32 should be tradable again: require structure, but avoid choking on tiny OFI samples.
    "set32_min_relative_volume": 1.15,
    "set32_require_direction_candle": True,
    "set32_require_ema50_side": True,
    "set32_require_orderflow_confirmation": True,
    "set32_orderflow_min_samples": 2,
    "set32_min_taker_ratio_long": 0.97,
    "set32_max_taker_ratio_short": 1.03,
    "set32_max_spread_pct": 0.09,

    # Keep scanner discovery broad, but block real entries when live market
    # quality stacks multiple adverse confirmations.
    "market_quality_long_hard_block_on_multi_adverse_enabled": True,
    "market_quality_long_multi_adverse_min_reasons": 4,
    "market_quality_long_multi_adverse_max_multiplier": 0.30,
    "market_quality_min_risk_multiplier": 0.0,
    "final_risk_multiplier_floor": 0.0,
    "aggressive_growth_enabled": False,
    "aggressive_growth_pyramiding_enabled": False,

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
    "profit_alpha_follow_through_enabled": True,
    "profit_alpha_default_follow_through_bars": 3,
    "profit_alpha_default_follow_through_min_mfe_r": 0.35,
    "profit_alpha_default_early_exit_max_mae_r": 0.75,
    "utbreakout_recent_loss_cooldown_enabled": True,
    "utbreakout_recent_loss_cooldown_seconds": 21600,
    "utbreakout_recent_loss_cooldown_min_loss_usdt": 0.0,

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

    # Loosen quality blocks enough for opportunity mode.
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
    "quality_score_v2_min_risk_multiplier": 0.60,
    "quality_score_v2_long_block_below": 12.0,
    "quality_score_v2_long_reduce_below": 40.0,
    "quality_score_v2_long_15m_block_below": 12.0,
    "quality_score_v2_long_15m_reduce_below": 40.0,
    "quality_score_v2_short_block_below": 16.0,
    "quality_score_v2_short_reduce_below": 45.0,
    "quality_score_v2_short_15m_block_below": 16.0,
    "quality_score_v2_short_15m_reduce_below": 45.0,

    # Continuation path: allow more valid continuation entries while still requiring trend/flow.
    "bias_continuation_min_volume_ratio": 0.40,
    "bias_continuation_15m_min_volume_ratio": 0.45,
    "bias_continuation_min_adaptive_tf_score": 30.0,
    "bias_continuation_15m_min_adaptive_tf_score": 32.0,
    "bias_continuation_min_adx": 10.0,
    "bias_continuation_15m_min_adx": 11.0,
    "bias_continuation_max_signal_age_candles": 10,
    "bias_continuation_15m_max_signal_age_candles": 10,
    "bias_continuation_max_signal_age": 10,
    "bias_continuation_15m_max_signal_age": 10,

    # More trades in opportunity mode, but still bounded.
    "max_daily_trades": 5,
    "max_consecutive_losses": 5,

    # Baseline TREND exit; the entry decision may replace it with a stronger
    # trend or squeeze profile at the live plan boundary.
    "take_profit_r_multiple": 2.40,
    "fixed_take_profit_enabled": True,
    "partial_take_profit_enabled": True,
    "partial_take_profit_r_multiple": 1.00,
    "partial_take_profit_ratio": 0.25,
    "second_take_profit_enabled": True,
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
    "adaptive_exit_ratio_min": 0.25,
    "adaptive_exit_ratio_max": 0.35,
    "adaptive_exit_trailing_multiplier_min": 2.4,
    "adaptive_exit_trailing_multiplier_max": 3.5,
    "adaptive_exit_activation_r_min": 1.0,
    "adaptive_exit_activation_r_max": 1.4,

    # Trend continuation sizing/capture: give valid continuation setups more room.
    "trend_continuation_entry_enabled": True,
    "trend_continuation_base_risk_multiplier": 0.75,
    "trend_continuation_min_risk_multiplier": 0.35,
    "trend_continuation_min_adx": 12.0,
    "trend_continuation_max_extension_atr": 2.60,
    "trend_continuation_flow_min_volume_ratio": 0.40,
    "trend_continuation_min_range_expansion": 1.01,
    "trend_continuation_quality_hard_floor": 16.0,
    "trend_continuation_quality_reduce_floor": 40.0,
    "trend_continuation_trend_hard_floor": 14.0,
    "trend_continuation_trend_reduce_floor": 38.0,
    "trend_continuation_strategy_hard_floor": 10.0,
    "trend_continuation_strategy_reduce_floor": 38.0,
}


def _as_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def apply_profit_opportunity_overrides(cfg):
    """Apply a profit-oriented live opportunity profile to effective config."""
    if isinstance(cfg, dict):
        cfg.update(PROFIT_MAX_OVERRIDES)
    return cfg


def enforce_core_hardening_config(cfg):
    """Keep legacy Set filters diagnostic under the single EV live router."""
    if isinstance(cfg, dict):
        cfg["selected_set_core_filter_hard_block_enabled"] = False
    return cfg


def tune_effective_config(cfg):
    cfg = apply_profit_opportunity_overrides(cfg)
    cfg = enforce_core_hardening_config(cfg)
    return cfg


def mark_core_set_filter_failures_hard(items, cfg):
    """Mark selected Set identity filter failures as hard-block compatible.

    Existing emas.py classifiers already hard-block names such as "유동성" and
    preserve the original rejection code when returning the final reason.  This
    patch maps only core Set failures to that hard-block-compatible name while
    keeping the original name/code in metadata and detail text.
    """
    if not isinstance(items, list):
        return items
    if not isinstance(cfg, dict):
        return items
    if not _as_bool(cfg.get("selected_set_core_filter_hard_block_enabled"), True):
        return items

    hardened = []
    for item in items:
        if not isinstance(item, dict):
            hardened.append(item)
            continue

        name = item.get("name") or ""
        code = item.get("code") or ""
        if item.get("state") is True:
            hardened.append(item)
            continue
        if name not in CORE_SET_FILTER_HARD_NAMES and code not in CORE_SET_FILTER_HARD_CODES:
            hardened.append(item)
            continue

        patched = dict(item)
        original_name = name or "선택 Set 핵심 필터"
        original_detail = patched.get("detail") or "core set filter failed"
        patched["core_set_hard_block"] = True
        patched["hard_block_original_name"] = original_name
        patched["hard_block_original_code"] = code
        patched["name"] = "유동성"
        patched["detail"] = f"{original_name}: {original_detail}; core set hard block"
        hardened.append(patched)

    return hardened


def patch_signal_engine(cls):
    current_get_cfg = getattr(cls, "_get_utbot_filtered_breakout_config", None)
    current_eval_filters = getattr(cls, "_evaluate_utbreakout_set_filter_items", None)

    wrapper_get = getattr(cls, "_utbreak_live_hardening_get_cfg_wrapper", None)
    wrapper_eval = getattr(cls, "_utbreak_live_hardening_eval_wrapper", None)

    changed = False

    if current_get_cfg is not None and current_get_cfg is not wrapper_get:
        original_get_cfg = current_get_cfg

        def tuned_get_utbreak_cfg(self, strategy_params=None):
            cfg = original_get_cfg(self, strategy_params)
            return tune_effective_config(cfg)

        cls._get_utbot_filtered_breakout_config = tuned_get_utbreak_cfg
        cls._utbreak_live_hardening_get_cfg_wrapper = tuned_get_utbreak_cfg
        changed = True

    if current_eval_filters is not None and current_eval_filters is not wrapper_eval:
        original_eval_filters = current_eval_filters

        def tuned_evaluate_utbreakout_set_filter_items(self, side, set_info, cfg, values):
            items = original_eval_filters(self, side, set_info, cfg, values)
            return mark_core_set_filter_failures_hard(items, cfg)

        cls._evaluate_utbreakout_set_filter_items = tuned_evaluate_utbreakout_set_filter_items
        cls._utbreak_live_hardening_eval_wrapper = tuned_evaluate_utbreakout_set_filter_items
        changed = True

    if changed:
        cls._utbreak_live_hardening_patch = True
        log.warning("UTBreak profit-opportunity hardening patch applied")
    return changed


def try_patch():
    for module_name in ("emas", "__main__"):
        module = sys.modules.get(module_name)
        cls = getattr(module, "SignalEngine", None) if module else None
        if cls is not None and patch_signal_engine(cls):
            return True
    return False


def install(timeout_seconds=60.0):
    def watch():
        deadline = time.time() + timeout_seconds
        applied_once = False
        while time.time() < deadline:
            if try_patch():
                applied_once = True
            # Keep watching a bit even after first patch so this wrapper can stay
            # last if another runtime patch wraps SignalEngine after us.
            if applied_once and time.time() > deadline - 5.0:
                return
            time.sleep(0.05)

    threading.Thread(target=watch, name="utbreak-live-hardening", daemon=True).start()

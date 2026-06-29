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
    "effective_profile_version": "ev_adaptive_v2",
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

    # Do not hard-kill long opportunities just because market quality is noisy;
    # keep normal risk-reduction logic instead.
    "market_quality_long_hard_block_on_multi_adverse_enabled": False,
    "market_quality_long_multi_adverse_min_reasons": 5,
    "market_quality_long_multi_adverse_max_multiplier": 0.35,
    "market_quality_min_risk_multiplier": 0.0,
    "final_risk_multiplier_floor": 0.0,
    "aggressive_growth_enabled": False,
    "aggressive_growth_pyramiding_enabled": False,

    "ev_min_entry_score": 55.0,
    "ev_min_net_expectancy_r": 0.08,
    "ev_entry_fee_rate_pct": 0.04,
    "ev_exit_fee_rate_pct": 0.04,
    "ev_slippage_rate_pct_each_side": 0.02,
    "ev_funding_buffer_pct": 0.01,
    "ev_cost_safety_multiplier": 1.25,
    "ev_max_spread_pct": 0.08,
    "ev_high_vol_atr_pct": 1.50,
    "ev_extreme_atr_pct": 2.50,
    "ev_panic_rebound_block_pct": 6.0,
    "ev_continuation_max_signal_age_bars": 10.0,
    "ev_continuation_reacceleration_range_min": 1.05,
    "ev_continuation_reacceleration_volume_min": 0.80,
    "ev_max_extension_atr": 2.40,
    "ev_preferred_extension_atr": 1.60,
    "ev_mtf_min_aligned": 2,
    "ev_leadership_bottom_block_pct": 15.0,
    "ev_conditional_relief_enabled": True,
    "ev_conditional_relief_risk_cap": 0.55,

    "ev_mtf_relief_enabled": True,
    "ev_mtf_relief_min_votes": 1,
    "ev_mtf_relief_min_score": 70.0,
    "ev_mtf_relief_min_adx": 26.0,
    "ev_mtf_relief_min_volume_ratio": 1.15,
    "ev_mtf_relief_min_efficiency": 0.28,

    "ev_stale_relief_enabled": True,
    "ev_stale_relief_max_age_bars": 24.0,
    "ev_stale_relief_min_score": 70.0,
    "ev_stale_relief_min_adx": 24.0,
    "ev_stale_relief_min_volume_ratio": 1.10,

    "ev_no_edge_relief_enabled": True,
    "ev_no_edge_relief_min_score": 67.0,
    "ev_no_edge_relief_min_adx": 23.0,
    "ev_no_edge_relief_min_volume_ratio": 1.05,
    "ev_no_edge_relief_min_efficiency": 0.24,
    "ev_no_edge_relief_min_range_expansion": 1.08,
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
    "max_daily_trades": 7,
    "max_consecutive_losses": 5,

    # Baseline TREND exit; the entry decision may replace it with a stronger
    # trend or squeeze profile at the live plan boundary.
    "take_profit_r_multiple": 2.00,
    "fixed_take_profit_enabled": True,
    "partial_take_profit_enabled": True,
    "partial_take_profit_r_multiple": 1.00,
    "partial_take_profit_ratio": 0.30,
    "second_take_profit_enabled": True,
    "second_take_profit_r_multiple": 2.00,
    "second_take_profit_ratio": 0.40,
    "runner_pct": 0.30,
    "dynamic_take_profit_enabled": False,
    "atr_trailing_activation_r": 1.00,
    "atr_trailing_multiplier": 2.70,
    "atr_trailing_enabled": True,
    "shadow_runner_exit_enabled": True,
    "runner_exit_enabled": True,
    "runner_chandelier_enabled": True,
    "runner_chandelier_multiplier": 2.70,
    "runner_chandelier_multiplier_max": 3.50,
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

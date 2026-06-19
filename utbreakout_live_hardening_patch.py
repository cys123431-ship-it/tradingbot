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

# Profit-opportunity profile: keep core overfit guards, but stop choking entries
# and push more of each winner toward TP2/runner capture.
PROFIT_MAX_OVERRIDES = {
    # Keep AUTO, but do not make the margin guard so strict that no trade appears.
    "selection_mode": "auto",
    "auto_select_enabled": True,
    "live_auto_set_whitelist_enabled": True,
    "live_auto_set_whitelist": [12, 22, 32, 51, 63],
    "auto_min_score_margin_live": 1.0,
    "auto_min_adjusted_score_live": 42.0,
    "auto_block_on_weak_margin_live": True,
    "auto_multiple_testing_penalty_enabled": True,
    "multiple_testing_free_trials": 20,
    "multiple_testing_max_score_penalty": 5.0,

    # Core Set identity still hard-blocks.  This is the main overfit guard to keep.
    "selected_set_core_filter_hard_block_enabled": True,
    "set_filter_soft_fail_enabled": True,
    "set_filter_soft_fail_multiplier": 0.80,
    "set_filter_multi_soft_fail_multiplier": 0.65,

    # Set32 should be tradable again: require structure, but avoid choking on tiny OFI samples.
    "set32_min_relative_volume": 1.20,
    "set32_require_direction_candle": True,
    "set32_require_ema50_side": True,
    "set32_require_orderflow_confirmation": True,
    "set32_orderflow_min_samples": 2,
    "set32_min_taker_ratio_long": 0.98,
    "set32_max_taker_ratio_short": 1.02,
    "set32_max_spread_pct": 0.08,

    # Do not hard-kill long opportunities just because market quality is noisy;
    # keep normal risk-reduction logic instead.
    "market_quality_long_hard_block_on_multi_adverse_enabled": False,
    "market_quality_long_multi_adverse_min_reasons": 5,
    "market_quality_long_multi_adverse_max_multiplier": 0.20,
    "market_quality_min_risk_multiplier": 0.30,

    # Loosen quality blocks enough for opportunity mode.
    "trend_health_hard_block_below": 16.0,
    "trend_health_reduce_below": 38.0,
    "trend_health_full_score": 66.0,
    "trend_health_min_multiplier": 0.40,
    "strategy_quality_hard_block_below": 10.0,
    "strategy_quality_reduce_below": 38.0,
    "strategy_quality_full_score": 68.0,
    "strategy_quality_min_multiplier": 0.40,
    "quality_score_v2_block_below": 16.0,
    "quality_score_v2_reduce_below": 45.0,
    "quality_score_v2_long_block_below": 16.0,
    "quality_score_v2_long_reduce_below": 45.0,
    "quality_score_v2_long_15m_block_below": 16.0,
    "quality_score_v2_long_15m_reduce_below": 45.0,
    "quality_score_v2_short_block_below": 20.0,
    "quality_score_v2_short_reduce_below": 52.0,
    "quality_score_v2_short_15m_block_below": 20.0,
    "quality_score_v2_short_15m_reduce_below": 52.0,

    # Continuation path: allow more valid continuation entries while still requiring trend/flow.
    "bias_continuation_min_volume_ratio": 0.45,
    "bias_continuation_15m_min_volume_ratio": 0.50,
    "bias_continuation_min_adaptive_tf_score": 32.0,
    "bias_continuation_15m_min_adaptive_tf_score": 34.0,
    "bias_continuation_min_adx": 12.0,
    "bias_continuation_15m_min_adx": 13.0,
    "bias_continuation_max_signal_age_candles": 8,
    "bias_continuation_15m_max_signal_age_candles": 8,
    "bias_continuation_max_signal_age": 8,
    "bias_continuation_15m_max_signal_age": 8,

    # More trades in opportunity mode, but still bounded.
    "max_daily_trades": 12,
    "max_consecutive_losses": 5,

    # Profit capture: smaller TP1, larger TP2, more runner room.
    "partial_take_profit_r_multiple": 1.00,
    "partial_take_profit_ratio": 0.25,
    "second_take_profit_r_multiple": 3.00,
    "second_take_profit_ratio": 0.35,
    "dynamic_tp2_base_r_multiple": 2.80,
    "dynamic_tp2_strong_r_multiple": 4.00,
    "dynamic_tp2_elite_r_multiple": 5.50,
    "atr_trailing_activation_r": 1.40,
    "atr_trailing_multiplier": 3.00,
    "runner_chandelier_multiplier": 3.30,

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
    """Keep the core overfit guard enabled in effective runtime config."""
    if isinstance(cfg, dict):
        cfg["selected_set_core_filter_hard_block_enabled"] = True
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

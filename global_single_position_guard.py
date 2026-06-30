"""Runtime guard and opportunity-mode tuning for crypto UTBreakout.

Loaded by scripts/launch_emas.py before emas.py is executed.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

log = logging.getLogger("one_position_guard")


OPPORTUNITY_PROFILE_NAME = "ev_adaptive_v2"

OPPORTUNITY_OVERRIDES = {
    "effective_profile_version": "ev_adaptive_v2",
    "ev_adaptive_enabled": True,
    "legacy_sets_research_only": True,

    # stable TF design
    "selection_mode": "auto",
    "auto_select_enabled": True,
    "adaptive_timeframe_enabled": True,
    "auto_timeframes": ["15m", "30m", "1h"],
    "adaptive_timeframes": ["15m", "30m", "1h"],
    "entry_timeframe": "15m",
    "exit_timeframe": "15m",
    "htf_timeframe": "1h",

    # AUTO opportunity: keep core overfit guards, but don't choke entries.
    "live_auto_set_whitelist_enabled": True,
    "live_auto_set_whitelist": [64],
    "active_set_id": 64,
    "profile": "set64",
    "safe_live_default_set_id": 64,
    "auto_block_on_weak_margin_live": False,
    "auto_multiple_testing_penalty_enabled": False,

    # less frequent timeframe switching
    "adaptive_timeframe_min_score": 36.0,
    "adaptive_timeframe_switch_margin": 8.0,
    "adaptive_timeframe_min_hold_candles": 5,

    # quality filters: loosen enough for profit opportunity mode.
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

    # continuation path: allow more continuation entries.
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

    # short filter: keep short possible, but not dead.
    "short_adx_threshold": 18.0,
    "short_dmi_min_gap": 1.5,
    "short_risk_multiplier": 0.65,

    # Set32: tradable again, while still keeping structure confirmation.
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
    "entry_quality_gate_min_final_risk_multiplier": 0.40,
    "entry_quality_gate_long_min_final_risk_multiplier": 0.45,
    "entry_quality_gate_short_min_final_risk_multiplier": 0.40,
    "entry_quality_gate_hard_market_multiplier_below": 0.30,
    "entry_quality_gate_min_ev_score": 60.0,
    "entry_quality_gate_min_ev_probability": 0.54,
    "entry_quality_gate_min_ev_net_expectancy_r": 0.22,
    "entry_quality_gate_min_ev_mtf_votes": 2,
    "utbreakout_recent_loss_cooldown_enabled": True,
    "utbreakout_recent_loss_cooldown_seconds": 21600,
    "utbreakout_recent_loss_cooldown_min_loss_usdt": 0.0,

    "ev_min_entry_score": 57.0,
    "ev_min_net_expectancy_r": 0.10,
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
    "ev_continuation_reacceleration_range_min": 1.08,
    "ev_continuation_reacceleration_volume_min": 0.95,
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
    "ev_stale_relief_min_score": 73.0,
    "ev_stale_relief_min_adx": 25.0,
    "ev_stale_relief_min_volume_ratio": 1.15,
    "ev_stale_relief_requires_reacceleration": True,

    "ev_no_edge_relief_enabled": True,
    "ev_no_edge_relief_min_score": 70.0,
    "ev_no_edge_relief_min_adx": 24.0,
    "ev_no_edge_relief_min_volume_ratio": 1.10,
    "ev_no_edge_relief_min_efficiency": 0.27,
    "ev_no_edge_relief_min_range_expansion": 1.10,
    "ev_short_min_entry_score": 57.0,
    "ev_short_trend_min_adx": 16.0,
    "ev_short_trend_min_volume_ratio": 0.60,
    "ev_short_no_edge_relief_min_score": 68.0,
    "ev_short_no_edge_relief_min_adx": 23.0,
    "ev_short_no_edge_relief_min_volume_ratio": 1.05,
    "ev_short_no_edge_relief_min_efficiency": 0.24,
    "ev_short_no_edge_relief_min_range_expansion": 1.08,
    "ev_short_conditional_relief_risk_cap": 0.30,
    "ev_short_relaxed_signal_risk_cap": 0.30,
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

    # Baseline TREND exit; stronger modes are applied per accepted entry.
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

    # bounded activity, but more opportunity than old profile.
    "max_daily_trades": 7,
    "max_consecutive_losses": 5,

    # Trend continuation entry path
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

    # Core Set identity remains hard-block; other soft failures only reduce size.
    "selected_set_core_filter_hard_block_enabled": False,
    "set_filter_soft_fail_enabled": False,
}


def key(symbol):
    return str(symbol or "").upper().replace(":USDT", "").replace("/", "").strip()


def active_position(engine, raw):
    if not isinstance(raw, dict):
        return None
    try:
        pos = engine._normalize_server_position(raw)
    except Exception:
        pos = raw
    if not isinstance(pos, dict):
        return None
    try:
        qty = abs(float(pos.get("contracts", 0) or 0))
    except Exception:
        qty = 0.0
    if qty <= 0:
        info = pos.get("info", {}) if isinstance(pos.get("info"), dict) else {}
        for field in ("positionAmt", "position_amt", "pa"):
            try:
                qty = abs(float(pos.get(field, info.get(field, 0)) or 0))
                if qty > 0:
                    break
            except Exception:
                pass
    return pos if qty > 0 else None


async def notify(engine, text):
    ctrl = getattr(engine, "ctrl", None)
    if not ctrl or not hasattr(ctrl, "notify"):
        return
    try:
        result = ctrl.notify(text)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


def _config_root(engine):
    ctrl = getattr(engine, "ctrl", None)
    cfg = getattr(ctrl, "cfg", None)
    root = getattr(cfg, "config", None)
    if isinstance(root, dict):
        return root
    if isinstance(cfg, dict):
        return cfg
    return None


def _persist_config_if_changed(engine, changed):
    if not changed:
        return False
    ctrl = getattr(engine, "ctrl", None)
    cfg = getattr(ctrl, "cfg", None)
    save = getattr(cfg, "save_config_sync", None)
    if not callable(save):
        return False
    try:
        result = save()
    except Exception as exc:
        log.error("UTBreak opportunity profile config persist failed: %s", exc)
        return False
    if result is False:
        log.error("UTBreak opportunity profile config persist returned False")
        return False
    log.warning("UTBreak opportunity profile persisted to config.json: %s", OPPORTUNITY_PROFILE_NAME)
    return True


def _is_utbreak_enabled(signal_cfg):
    if not isinstance(signal_cfg, dict):
        return False
    common = signal_cfg.get("common_settings", {}) if isinstance(signal_cfg.get("common_settings"), dict) else {}
    selector = signal_cfg.get("coin_selector", {}) if isinstance(signal_cfg.get("coin_selector"), dict) else {}
    strategy = signal_cfg.get("strategy_params", {}) if isinstance(signal_cfg.get("strategy_params"), dict) else {}
    active = str(strategy.get("active_strategy", "")).upper()
    return (
        "UTBOT_ADAPTIVE" in active
        or "UTBREAK" in active
        or bool(common.get("scanner_enabled"))
        or bool(selector.get("enabled"))
    )


def _set_if_different(container, key_name, value):
    if not isinstance(container, dict):
        return False
    if container.get(key_name) != value:
        container[key_name] = value
        return True
    return False


def apply_opportunity_tuning(engine):
    """Make UTBreakout more opportunity-oriented while keeping one-position safety."""

    root = _config_root(engine)
    if not isinstance(root, dict):
        return False

    signal = root.setdefault("signal_engine", {})
    if not _is_utbreak_enabled(signal):
        return False

    common = signal.setdefault("common_settings", {})
    selector = signal.setdefault("coin_selector", {})
    strategy = signal.setdefault("strategy_params", {})
    ut = strategy.setdefault("UTBotFilteredBreakoutV1", {})

    changed = False

    # Scanner: scan faster and do not require a large move before a candidate is considered.
    changed |= _set_if_different(common, "scanner_enabled", True)
    changed |= _set_if_different(common, "scanner_timeframe", "5m")
    changed |= _set_if_different(common, "scanner_exit_timeframe", "15m")
    changed |= _set_if_different(common, "scanner_min_rise_pct", 0.20)
    changed |= _set_if_different(common, "scanner_max_rise_pct", 15.0)

    # CoinSelector: lower the discovery threshold and refresh more often.
    selector_updates = {
        "enabled": True,
        "analysis_limit": 80,
        "top_n": 20,
        "min_final_score": 45.0,
        "min_quote_volume_usdt": 25_000_000.0,
        "ideal_quote_volume_usdt": 250_000_000.0,
        "min_trade_count": 5_000,
        "ideal_trade_count": 120_000,
        "max_spread_pct": 0.12,
        "max_abs_price_change_pct": 24.0,
        "refresh_interval_seconds": 90,
        "candidate_cooldown_enabled": False,
        "custom_relax_discovery": True,
        "selection_quality_enabled": True,
        "selection_max_rebound_pct": 22.0,
    }
    for k, v in selector_updates.items():
        changed |= _set_if_different(selector, k, v)

    # DOGE-style high-beta coins should not be silently filtered when the user chose them.
    excluded = selector.get("excluded_sectors")
    if isinstance(excluded, list) and "meme" in excluded:
        selector["excluded_sectors"] = [item for item in excluded if item != "meme"]
        changed = True

    # Adaptive strategy: use OPPORTUNITY_OVERRIDES for faster discovery and bigger winner capture.
    for k, v in OPPORTUNITY_OVERRIDES.items():
        changed |= _set_if_different(ut, k, v)
    if changed:
        log.warning("UTBreak opportunity profile applied: %s", OPPORTUNITY_PROFILE_NAME)
        _persist_config_if_changed(engine, changed)
    return changed


def patch_signal_engine(cls):
    if getattr(cls, "_global_one_position_guard", False):
        return False

    original_entry = getattr(cls, "entry", None)
    original_start = getattr(cls, "start", None)
    original_get_cfg = getattr(cls, "_get_utbot_filtered_breakout_config", None)

    if original_entry is None or original_start is None:
        return False

    def tuned_get_utbreak_cfg(self, strategy_params=None):
        try:
            apply_opportunity_tuning(self)
        except Exception as exc:
            log.error("UTBreak direction-filter tuning failed before cfg: %s", exc)

        if original_get_cfg is None:
            cfg = strategy_params or {}
        else:
            cfg = original_get_cfg(self, strategy_params)

        if isinstance(cfg, dict):
            cfg.update(OPPORTUNITY_OVERRIDES)

        return cfg

    def tuned_start(self, *args, **kwargs):
        try:
            apply_opportunity_tuning(self)
        except Exception as exc:
            log.error("UTBreak opportunity tuning failed: %s", exc)
        return original_start(self, *args, **kwargs)

    async def guarded_entry(self, symbol, side, price, *args, **kwargs):
        try:
            apply_opportunity_tuning(self)
        except Exception as exc:
            log.error("UTBreak opportunity tuning failed before entry: %s", exc)

        lock = getattr(self, "_global_one_position_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, "_global_one_position_lock", lock)
        async with lock:
            target = key(symbol)
            try:
                positions = await asyncio.to_thread(self.exchange.fetch_positions)
                if not isinstance(positions, list):
                    positions = []
                for raw in positions:
                    pos = active_position(self, raw)
                    if not pos:
                        continue
                    info = pos.get("info", {}) if isinstance(pos.get("info"), dict) else {}
                    held_symbol = pos.get("symbol") or info.get("symbol") or "unknown"
                    if key(held_symbol) != target:
                        reason = f"전체 동시 포지션 1개 제한: 보유 중 {held_symbol}"
                        try:
                            self.last_entry_reason[symbol] = reason
                        except Exception:
                            pass
                        log.warning("entry blocked by one-position guard: %s %s; holding %s", symbol, side, held_symbol)
                        await notify(self, f"⚠️ 진입 차단: {reason}")
                        return None
            except Exception as exc:
                log.error("one-position guard position check failed: %s", exc)
                await notify(self, f"⚠️ 진입 차단: 포지션 확인 실패 ({exc})")
                return None
            return await original_entry(self, symbol, side, price, *args, **kwargs)

    if original_get_cfg is not None:
        cls._get_utbot_filtered_breakout_config = tuned_get_utbreak_cfg

    cls.start = tuned_start
    cls.entry = guarded_entry
    cls._global_one_position_guard = True
    log.warning("global one-position guard and UTBreak opportunity tuning applied")
    return True


def try_patch():
    for name in ("emas", "__main__"):
        module = sys.modules.get(name)
        cls = getattr(module, "SignalEngine", None) if module else None
        if cls is not None and patch_signal_engine(cls):
            return True
    return False


def install(timeout_seconds=60.0):
    def watch():
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if try_patch():
                return
            time.sleep(0.02)
    threading.Thread(target=watch, name="one-position-guard", daemon=True).start()

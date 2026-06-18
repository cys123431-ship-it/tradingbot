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


OPPORTUNITY_PROFILE_NAME = "utbreak_direction_filter_v2_continuation"

OPPORTUNITY_OVERRIDES = {
    # stable TF design
    "selection_mode": "auto",
    "auto_select_enabled": True,
    "adaptive_timeframe_enabled": True,
    "auto_timeframes": ["15m", "30m", "1h"],
    "adaptive_timeframes": ["15m", "30m", "1h"],
    "entry_timeframe": "15m",
    "exit_timeframe": "15m",
    "htf_timeframe": "1h",

    # less frequent timeframe switching
    "adaptive_timeframe_min_score": 38.0,
    "adaptive_timeframe_switch_margin": 10.0,
    "adaptive_timeframe_min_hold_candles": 6,

    # reduce overfitted hard blocks
    "trend_health_hard_block_below": 20.0,
    "trend_health_reduce_below": 42.0,
    "trend_health_full_score": 68.0,
    "trend_health_min_multiplier": 0.35,

    "strategy_quality_hard_block_below": 12.0,
    "strategy_quality_reduce_below": 42.0,
    "strategy_quality_full_score": 70.0,
    "strategy_quality_min_multiplier": 0.35,

    "quality_score_v2_block_below": 20.0,
    "quality_score_v2_reduce_below": 50.0,

    "quality_score_v2_long_block_below": 20.0,
    "quality_score_v2_long_reduce_below": 50.0,
    "quality_score_v2_long_15m_block_below": 20.0,
    "quality_score_v2_long_15m_reduce_below": 50.0,

    "quality_score_v2_short_block_below": 25.0,
    "quality_score_v2_short_reduce_below": 60.0,
    "quality_score_v2_short_15m_block_below": 25.0,
    "quality_score_v2_short_15m_reduce_below": 60.0,

    # volume should rarely be the only hard block
    "bias_continuation_min_volume_ratio": 0.50,
    "bias_continuation_15m_min_volume_ratio": 0.55,
    "bias_continuation_min_adaptive_tf_score": 35.0,
    "bias_continuation_15m_min_adaptive_tf_score": 38.0,
    "bias_continuation_min_adx": 14.0,
    "bias_continuation_15m_min_adx": 15.0,

    # short filter: stricter than long, but not dead
    "short_adx_threshold": 20.0,
    "short_dmi_min_gap": 2.0,
    "short_risk_multiplier": 0.60,

    # exits
    "partial_take_profit_r_multiple": 1.20,
    "partial_take_profit_ratio": 0.35,
    "second_take_profit_r_multiple": 2.50,
    "second_take_profit_ratio": 0.35,
    "dynamic_tp2_base_r_multiple": 2.30,
    "dynamic_tp2_strong_r_multiple": 3.00,
    "dynamic_tp2_elite_r_multiple": 4.00,
    "atr_trailing_activation_r": 1.20,
    "atr_trailing_multiplier": 2.50,
    "runner_chandelier_multiplier": 2.80,

    # bounded activity
    "max_daily_trades": 8,
    "max_consecutive_losses": 4,

    # Trend continuation entry path
    "trend_continuation_entry_enabled": True,
    "trend_continuation_base_risk_multiplier": 0.60,
    "trend_continuation_min_risk_multiplier": 0.25,
    "trend_continuation_min_adx": 14.0,
    "trend_continuation_max_extension_atr": 2.20,
    "trend_continuation_flow_min_volume_ratio": 0.45,
    "trend_continuation_min_range_expansion": 1.03,
    "trend_continuation_quality_hard_floor": 20.0,
    "trend_continuation_quality_reduce_floor": 45.0,
    "trend_continuation_trend_hard_floor": 18.0,
    "trend_continuation_trend_reduce_floor": 42.0,
    "trend_continuation_strategy_hard_floor": 12.0,
    "trend_continuation_strategy_reduce_floor": 42.0,

    # Set filter failures should mostly reduce size, not block continuation entries
    "set_filter_soft_fail_enabled": True,
    "set_filter_soft_fail_multiplier": 0.70,
    "set_filter_multi_soft_fail_multiplier": 0.50,
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
    """Make UTBreakout less under-active while keeping one-position safety.

    The goal is not to guarantee profit. It increases the chance of taking the
    strongest breakout by relaxing selector/filter thresholds and opening the
    runner side of exits, while retaining global one-position protection.
    """


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
        "analysis_limit": 60,
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
    }
    for k, v in selector_updates.items():
        changed |= _set_if_different(selector, k, v)

    # DOGE-style high-beta coins should not be silently filtered when the user chose them.
    excluded = selector.get("excluded_sectors")
    if isinstance(excluded, list) and "meme" in excluded:
        selector["excluded_sectors"] = [item for item in excluded if item != "meme"]
        changed = True

    # Adaptive strategy: use OPPORTUNITY_OVERRIDES for faster discovery,
    # lower blocks, and let winners run more.
    for k, v in OPPORTUNITY_OVERRIDES.items():
        changed |= _set_if_different(ut, k, v)
    if changed:
        log.warning("UTBreak opportunity profile applied: %s", OPPORTUNITY_PROFILE_NAME)
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
            cfg.update({
                "auto_timeframes": ["15m", "30m", "1h"],
                "adaptive_timeframes": ["15m", "30m", "1h"],
                "entry_timeframe": "15m",
                "exit_timeframe": "15m",
                "htf_timeframe": "1h",

                "adaptive_timeframe_min_score": 38.0,
                "adaptive_timeframe_switch_margin": 10.0,
                "adaptive_timeframe_min_hold_candles": 6,

                "bias_continuation_min_volume_ratio": 0.50,
                "bias_continuation_15m_min_volume_ratio": 0.55,
                "bias_continuation_min_adaptive_tf_score": 35.0,
                "bias_continuation_15m_min_adaptive_tf_score": 38.0,
                "bias_continuation_min_adx": 14.0,
                "bias_continuation_15m_min_adx": 15.0,

                "trend_health_hard_block_below": 20.0,
                "trend_health_reduce_below": 42.0,
                "strategy_quality_hard_block_below": 12.0,
                "strategy_quality_reduce_below": 42.0,

                "quality_score_v2_block_below": 20.0,
                "quality_score_v2_reduce_below": 50.0,
                "quality_score_v2_long_block_below": 20.0,
                "quality_score_v2_long_reduce_below": 50.0,
                "quality_score_v2_long_15m_block_below": 20.0,
                "quality_score_v2_long_15m_reduce_below": 50.0,
                "quality_score_v2_short_block_below": 25.0,
                "quality_score_v2_short_reduce_below": 60.0,
                "quality_score_v2_short_15m_block_below": 25.0,
                "quality_score_v2_short_15m_reduce_below": 60.0,

                "partial_take_profit_r_multiple": 1.20,
                "second_take_profit_r_multiple": 2.50,
                "dynamic_tp2_base_r_multiple": 2.30,
                "dynamic_tp2_strong_r_multiple": 3.00,
                "dynamic_tp2_elite_r_multiple": 4.00,
                "atr_trailing_activation_r": 1.20,
                "atr_trailing_multiplier": 2.50,

                "trend_continuation_entry_enabled": True,
                "trend_continuation_base_risk_multiplier": 0.60,
                "trend_continuation_min_risk_multiplier": 0.25,
                "trend_continuation_min_adx": 14.0,
                "trend_continuation_max_extension_atr": 2.20,
                "trend_continuation_flow_min_volume_ratio": 0.45,
                "trend_continuation_min_range_expansion": 1.03,
                "trend_continuation_quality_hard_floor": 20.0,
                "trend_continuation_quality_reduce_floor": 45.0,
                "trend_continuation_trend_hard_floor": 18.0,
                "trend_continuation_trend_reduce_floor": 42.0,
                "trend_continuation_strategy_hard_floor": 12.0,
                "trend_continuation_strategy_reduce_floor": 42.0,

                "set_filter_soft_fail_enabled": True,
                "set_filter_soft_fail_multiplier": 0.70,
                "set_filter_multi_soft_fail_multiplier": 0.50,
            })

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

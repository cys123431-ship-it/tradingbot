"""Runtime UTBreakout live hardening patch.

This module is intentionally small and loaded from scripts/launch_emas.py before
emas.py is executed.  It avoids replacing the very large emas.py file while still
ensuring selected Set identity filter failures become true hard blocks in the
Azure/Telegram runtime path.
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


def enforce_core_hardening_config(cfg):
    """Keep the live hardening flag enabled in effective runtime config."""
    if isinstance(cfg, dict):
        cfg["selected_set_core_filter_hard_block_enabled"] = True
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
    if getattr(cls, "_utbreak_live_hardening_patch", False):
        return False

    original_get_cfg = getattr(cls, "_get_utbot_filtered_breakout_config", None)
    original_eval_filters = getattr(cls, "_evaluate_utbreakout_set_filter_items", None)

    if original_get_cfg is None and original_eval_filters is None:
        return False

    if original_get_cfg is not None:
        def tuned_get_utbreak_cfg(self, strategy_params=None):
            cfg = original_get_cfg(self, strategy_params)
            return enforce_core_hardening_config(cfg)

        cls._get_utbot_filtered_breakout_config = tuned_get_utbreak_cfg

    if original_eval_filters is not None:
        def tuned_evaluate_utbreakout_set_filter_items(self, side, set_info, cfg, values):
            items = original_eval_filters(self, side, set_info, cfg, values)
            return mark_core_set_filter_failures_hard(items, cfg)

        cls._evaluate_utbreakout_set_filter_items = tuned_evaluate_utbreakout_set_filter_items

    cls._utbreak_live_hardening_patch = True
    log.warning("UTBreak live core-set hardening patch applied")
    return True


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
        while time.time() < deadline:
            if try_patch():
                return
            time.sleep(0.02)

    threading.Thread(target=watch, name="utbreak-live-hardening", daemon=True).start()

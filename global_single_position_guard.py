"""Global one-position guard for crypto entry calls.

Imported by sitecustomize at process startup.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

log = logging.getLogger("one_position_guard")


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


def patch_signal_engine(cls):
    if getattr(cls, "_global_one_position_guard", False):
        return False
    original = getattr(cls, "entry", None)
    if original is None:
        return False

    async def guarded_entry(self, symbol, side, price, *args, **kwargs):
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
            return await original(self, symbol, side, price, *args, **kwargs)

    cls.entry = guarded_entry
    cls._global_one_position_guard = True
    log.warning("global one-position guard applied to SignalEngine.entry")
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

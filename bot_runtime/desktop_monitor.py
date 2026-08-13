"""Small, sanitized runtime snapshot used by the native desktop monitor.

This module never calls the exchange and never places orders.  It only copies a
bounded subset of controller/engine state to ``runtime/desktop_monitor.json``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime


_BRANCHES = (
    ("utbreak", "UTBreak"),
    ("rspt", "RSPT-v3"),
    ("vmt", "VMT Trend"),
    ("crowding_unwind", "Crowding Unwind"),
    ("lxr", "LXR Reversal"),
)

_ACTIVE_STATUS_STORES = {
    "volatility_managed_trend_v1": "volatility_managed_trend_last_status",
    "adaptive_breakout_trend_v1": "adaptive_breakout_trend_last_status",
    "funding_oi_crowding_unwind_v1": "crowding_unwind_last_status",
    "liquidation_exhaustion_reversal_v1": "liquidation_exhaustion_reversal_last_status",
}


def _safe_text(value, limit=320):
    text = str(value or "").replace("\x00", " ").strip()
    return text[:limit]


def _safe_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _canonical_symbol(value):
    return _safe_text(value, 80).replace(":USDT", "")


def _get_symbol(controller, engine):
    candidates = []
    candidates.append(getattr(engine, "scanner_active_symbol", None))
    status_data = getattr(controller, "status_data", {})
    if isinstance(status_data, dict):
        for key, value in status_data.items():
            if key not in {"PAUSED", "SCANNER"}:
                candidates.append(value.get("symbol") if isinstance(value, dict) else key)
    candidates.append(getattr(engine, "current_utbreakout_candidate_symbol", None))
    current_symbol = getattr(controller, "_get_current_symbol", None)
    if callable(current_symbol):
        try:
            candidates.append(current_symbol())
        except Exception:
            pass
    for value in candidates:
        symbol = _canonical_symbol(value)
        if symbol and "SCANN" not in symbol.upper() and symbol.upper() != "PAUSED":
            return symbol
    return None


def _status_for_symbol(store, symbol):
    if not isinstance(store, dict) or not store:
        return {}
    aliases = {
        _safe_text(symbol),
        _canonical_symbol(symbol),
        f"{_canonical_symbol(symbol)}:USDT" if symbol else "",
    }
    for key, value in store.items():
        if _safe_text(key) in aliases or _canonical_symbol(key) in aliases:
            return value if isinstance(value, dict) else {}
    latest = next(reversed(store.values()))
    return latest if isinstance(latest, dict) else {}


def _row_from_light(key, fallback_label, light):
    light = light if isinstance(light, dict) else {}
    state = _safe_text(light.get("light") or light.get("state") or "unknown", 20).lower()
    state = {
        "green": "valid",
        "ready": "valid",
        "red": "rejected",
        "blocked": "rejected",
        "yellow": "waiting",
        "wait": "waiting",
        "gray": "unknown",
        "none": "unknown",
    }.get(state, state)
    if light.get("disabled") or state == "off":
        state = "off"
    return {
        "key": key,
        "name": _safe_text(light.get("label") or fallback_label, 64),
        "state": state or "unknown",
        "side": _safe_text(light.get("side"), 10).upper() or None,
        "reason": _safe_text(light.get("reason") or "최근 평가 없음"),
    }


def _row_from_status(key, name, status, enabled=True):
    status = status if isinstance(status, dict) else {}
    if not enabled:
        return {"key": key, "name": name, "state": "off", "side": None, "reason": "OFF"}
    side = _safe_text(
        status.get("accepted_side")
        or status.get("candidate_side")
        or status.get("candidate_signal"),
        10,
    ).upper() or None
    stage = _safe_text(status.get("stage"), 32).lower()
    if side in {"LONG", "SHORT"} and status.get("accepted_code") == "ACCEPTED_ENTRY":
        state = "valid"
    elif status.get("reject_code") or stage in {"rejected", "blocked"}:
        state = "rejected"
    elif status:
        state = "waiting"
    else:
        state = "unknown"
    return {
        "key": key,
        "name": name,
        "state": state,
        "side": side,
        "reason": _safe_text(status.get("reason") or status.get("reject_code") or "최근 평가 없음"),
    }


def _strategy_rows(engine, active_strategy, strategy_params, symbol):
    quad_status = _status_for_symbol(getattr(engine, "quad_alpha_last_status", {}), symbol)
    quad = quad_status.get("quad_alpha") if isinstance(quad_status, dict) else None
    if active_strategy == "quad_alpha_v1" or isinstance(quad, dict):
        enabled = set(
            strategy_params.get("UTBotFilteredBreakoutV1", {}).get(
                "quad_alpha_enabled_strategies", ()
            )
            or ()
        )
        rows = []
        for key, label in _BRANCHES:
            light = quad.get(key) if isinstance(quad, dict) else None
            row = _row_from_light(key, label, light)
            if enabled and not light:
                aliases = {
                    "utbreak": "ut_breakout",
                    "rspt": "relative_strength_pullback_trend",
                    "vmt": "volatility_managed_trend_v1",
                    "crowding_unwind": "funding_oi_crowding_unwind_v1",
                    "lxr": "liquidation_exhaustion_reversal_v1",
                }
                if aliases[key] not in enabled:
                    row = _row_from_status(key, label, {}, enabled=False)
            rows.append(row)
        return rows

    store_name = _ACTIVE_STATUS_STORES.get(
        active_strategy,
        "last_utbot_filtered_breakout_status",
    )
    status = _status_for_symbol(getattr(engine, store_name, {}), symbol)
    return [
        _row_from_status(
            active_strategy or "unknown",
            _safe_text(status.get("strategy") or active_strategy or "Strategy", 64),
            status,
        )
    ]


def _position_hints(engine):
    states = getattr(engine, "utbreakout_trailing_states", {})
    if not isinstance(states, dict):
        return []
    hints = []
    seen = set()
    for raw_symbol, state in list(states.items())[-20:]:
        if not isinstance(state, dict):
            continue
        symbol = _canonical_symbol(raw_symbol)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        hints.append(
            {
                "symbol": symbol,
                "source": "BOT",
                "strategy": _safe_text(state.get("strategy") or "BOT", 80),
                "entry_price": _safe_number(state.get("entry_price")),
                "stop_price": _safe_number(
                    state.get("last_stop_price") or state.get("initial_stop_price")
                ),
                "tp_prices": [
                    value
                    for value in (
                        _safe_number(item.get("price"))
                        for item in (state.get("planned_tp_orders") or [])[:4]
                        if isinstance(item, dict)
                    )
                    if value is not None
                ],
            }
        )
    return hints


def build_desktop_monitor_snapshot(controller):
    engines = getattr(controller, "engines", {})
    engine = engines.get("signal") if isinstance(engines, dict) else None
    strategy_params = {}
    get_params = getattr(controller, "get_active_strategy_params", None)
    if callable(get_params):
        try:
            strategy_params = get_params() or {}
        except Exception:
            strategy_params = {}
    active_strategy = _safe_text(strategy_params.get("active_strategy") or "unknown", 80).lower()
    symbol = _get_symbol(controller, engine) if engine is not None else None

    mode = "unknown"
    get_mode = getattr(controller, "get_exchange_mode", None)
    if callable(get_mode):
        try:
            mode = _safe_text(get_mode(), 40)
        except Exception:
            pass

    status_rows = []
    status_data = getattr(controller, "status_data", {})
    if isinstance(status_data, dict):
        for key, value in list(status_data.items())[:12]:
            if not isinstance(value, dict):
                continue
            status_rows.append(
                {
                    "key": _safe_text(key, 80),
                    "symbol": _canonical_symbol(value.get("symbol") or key),
                    "side": _safe_text(value.get("pos_side"), 12).upper() or "NONE",
                    "price": _safe_number(value.get("price")),
                    "entry_reason": _safe_text(value.get("entry_reason")),
                    "equity": _safe_number(value.get("total_equity")),
                    "free_usdt": _safe_number(value.get("free_usdt")),
                    "daily_pnl": _safe_number(value.get("daily_pnl")),
                }
            )

    return {
        "schema_version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "epoch": int(time.time()),
        "bot": {
            "paused": bool(getattr(controller, "is_paused", False)),
            "exchange_mode": mode,
            "active_strategy": active_strategy,
            "current_symbol": symbol,
            "scanner_active_symbol": _canonical_symbol(
                getattr(engine, "scanner_active_symbol", None)
            ) or None,
        },
        "strategies": (
            _strategy_rows(engine, active_strategy, strategy_params, symbol)
            if engine is not None
            else []
        ),
        "position_hints": _position_hints(engine) if engine is not None else [],
        "status_rows": status_rows,
    }


def write_desktop_monitor_snapshot(controller):
    runtime_dir = getattr(controller, "runtime_dir", None) or os.path.join(
        getattr(controller, "base_dir", "."), "runtime"
    )
    os.makedirs(runtime_dir, exist_ok=True)
    target = os.path.join(runtime_dir, "desktop_monitor.json")
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(
            build_desktop_monitor_snapshot(controller),
            handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        handle.flush()
    os.replace(temporary, target)
    return target


__all__ = ("build_desktop_monitor_snapshot", "write_desktop_monitor_snapshot")

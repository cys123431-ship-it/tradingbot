"""Normalization and display helpers for configurable UT filter packs."""

from __future__ import annotations


UTBOT_FILTER_PACK_LABELS = {
    1: "CHOP",
    2: "ADX+DMI",
    3: "VWAP",
    4: "HTF Supertrend",
    5: "BOS/CHoCH",
}
UTBOT_FILTER_PACK_ID_SET = frozenset(UTBOT_FILTER_PACK_LABELS)
ALT_TREND_ALLOWED_TIMEFRAMES = (
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "1d",
)
ALT_TREND_TIMEFRAME_ORDER = {
    timeframe: index
    for index, timeframe in enumerate(ALT_TREND_ALLOWED_TIMEFRAMES)
}


def get_utbot_filter_pack_label(filter_id):
    try:
        return UTBOT_FILTER_PACK_LABELS[int(filter_id)]
    except (TypeError, ValueError, KeyError):
        return f"Filter {filter_id}"


def normalize_utbot_filter_pack_selected(selected):
    if selected is None:
        return []
    if isinstance(selected, str):
        raw_items = [item.strip() for item in selected.split(",")]
    elif isinstance(selected, (list, tuple, set)):
        raw_items = list(selected)
    else:
        raw_items = [selected]
    normalized = []
    seen = set()
    for raw_item in raw_items:
        item_text = str(raw_item or "").strip().lower()
        if not item_text or item_text in {"0", "off", "none", "[]"}:
            continue
        try:
            filter_id = int(item_text)
        except (TypeError, ValueError):
            continue
        if filter_id in UTBOT_FILTER_PACK_ID_SET and filter_id not in seen:
            normalized.append(filter_id)
            seen.add(filter_id)
    return sorted(normalized)


def normalize_utbot_filter_pack_logic(logic):
    return "or" if str(logic or "and").strip().lower() == "or" else "and"


def normalize_utbot_filter_pack_exit_mode(mode):
    aliases = {
        "c": "confirm",
        "confirm": "confirm",
        "s": "signal",
        "signal": "signal",
    }
    return aliases.get(str(mode or "confirm").strip().lower(), "confirm")


def normalize_utbot_filter_pack_config(filter_pack):
    raw_pack = filter_pack if isinstance(filter_pack, dict) else {}
    entry_cfg = (
        raw_pack.get("entry")
        if isinstance(raw_pack.get("entry"), dict)
        else {}
    )
    exit_cfg = (
        raw_pack.get("exit")
        if isinstance(raw_pack.get("exit"), dict)
        else {}
    )
    entry_selected = normalize_utbot_filter_pack_selected(
        entry_cfg.get("selected", [])
    )
    exit_selected = normalize_utbot_filter_pack_selected(
        exit_cfg.get("selected", [])
    )
    raw_mode_by_filter = exit_cfg.get("mode_by_filter")
    normalized_mode_by_filter = {}
    if isinstance(raw_mode_by_filter, dict):
        for filter_id in exit_selected:
            raw_mode = raw_mode_by_filter.get(
                str(filter_id),
                raw_mode_by_filter.get(filter_id, "confirm"),
            )
            normalized_mode_by_filter[str(filter_id)] = (
                normalize_utbot_filter_pack_exit_mode(raw_mode)
            )
    else:
        for filter_id in exit_selected:
            normalized_mode_by_filter[str(filter_id)] = "confirm"
    for filter_id in exit_selected:
        normalized_mode_by_filter.setdefault(str(filter_id), "confirm")
    return {
        "entry": {
            "selected": entry_selected,
            "logic": normalize_utbot_filter_pack_logic(
                entry_cfg.get("logic", "and")
            ),
        },
        "exit": {
            "selected": exit_selected,
            "logic": normalize_utbot_filter_pack_logic(
                exit_cfg.get("logic", "and")
            ),
            "mode_by_filter": normalized_mode_by_filter,
        },
    }


def format_utbot_filter_pack_selected(selected):
    normalized = normalize_utbot_filter_pack_selected(selected)
    if not normalized:
        return "OFF"
    return "[" + ",".join(str(filter_id) for filter_id in normalized) + "]"


def format_utbot_filter_pack_logic(logic):
    return normalize_utbot_filter_pack_logic(logic).upper()


def format_utbot_filter_pack_mode_map(mode_by_filter, selected=None):
    normalized_selected = normalize_utbot_filter_pack_selected(selected or [])
    if not normalized_selected:
        return "OFF"
    raw_map = mode_by_filter if isinstance(mode_by_filter, dict) else {}
    items = []
    for filter_id in normalized_selected:
        mode = normalize_utbot_filter_pack_exit_mode(
            raw_map.get(str(filter_id), raw_map.get(filter_id, "confirm"))
        )
        items.append(f"{filter_id}={'C' if mode == 'confirm' else 'S'}")
    return ", ".join(items) if items else "OFF"


def normalize_alt_trend_timeframes(timeframes):
    if isinstance(timeframes, str):
        raw_tokens = [
            token.strip().lower()
            for token in timeframes.split(",")
            if token.strip()
        ]
    elif isinstance(timeframes, (list, tuple, set)):
        raw_tokens = [
            str(token or "").strip().lower()
            for token in timeframes
            if str(token or "").strip()
        ]
    else:
        try:
            raw_tokens = [
                str(token or "").strip().lower()
                for token in list(timeframes)
                if str(token or "").strip()
            ]
        except Exception:
            raw_tokens = []
    selected = []
    seen = set()
    for token in raw_tokens:
        if token not in ALT_TREND_TIMEFRAME_ORDER or token in seen:
            continue
        seen.add(token)
        selected.append(token)
    selected.sort(
        key=lambda timeframe: ALT_TREND_TIMEFRAME_ORDER.get(timeframe, 999)
    )
    return selected


def format_alt_trend_timeframes(timeframes):
    normalized = normalize_alt_trend_timeframes(timeframes)
    return ", ".join(normalized) if normalized else "OFF"

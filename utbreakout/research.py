"""Research summaries for UT Breakout diagnostic JSONL logs."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math


def _parse_ts(value):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_float(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _avg(values):
    clean = [_safe_float(v) for v in values]
    clean = [v for v in clean if v is not None]
    return sum(clean) / len(clean) if clean else None


def load_diagnostic_events(paths, days=7, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(days or 7)))
    events = []
    for path in paths or []:
        try:
            with open(path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_ts = _parse_ts(event.get("ts"))
                    if event_ts and event_ts >= cutoff:
                        event = dict(event)
                        event["_dt"] = event_ts
                        events.append(event)
        except OSError:
            continue
    events.sort(key=lambda item: item.get("_dt") or datetime.min.replace(tzinfo=timezone.utc))
    return events


def summarize_diagnostic_events(events, protection_status=None):
    events = list(events or [])
    candidate_keys = {
        (
            event.get("symbol"),
            event.get("side"),
            event.get("decision_candle_ts") or event.get("ut_signal_ts") or event.get("ts"),
        )
        for event in events
    }
    rejected = [event for event in events if str(event.get("code") or "").startswith("REJECTED_")]
    accepted = [event for event in events if event.get("code") == "ACCEPTED_ENTRY"]
    blocked = [event for event in events if event.get("event") == "entry_blocked"]
    set_counts = Counter(
        f"Set{int(event.get('auto_selected_set_id'))}"
        for event in events
        if str(event.get("auto_selected_set_id") or "").replace(".", "", 1).isdigit()
    )
    reject_counts = Counter(event.get("code") or "UNKNOWN" for event in rejected)
    side_counts = Counter(str(event.get("side") or "unknown").upper() for event in events)
    candidate_type_counts = Counter(event.get("candidate_type") or "unknown" for event in events)

    total_set_events = sum(set_counts.values())
    top_set, top_set_count = set_counts.most_common(1)[0] if set_counts else (None, 0)
    top_set_share = (top_set_count / total_set_events * 100.0) if total_set_events else 0.0

    risk_distance_pct_values = []
    for event in events:
        risk_pct = _safe_float(event.get("risk_distance_pct"))
        if risk_pct is None:
            risk_distance = _safe_float(event.get("risk_distance"))
            entry_price = _safe_float(event.get("entry_price"))
            if risk_distance is not None and entry_price and entry_price > 0:
                risk_pct = risk_distance / entry_price * 100.0
        if risk_pct is not None:
            risk_distance_pct_values.append(risk_pct)

    protection_status = protection_status or {}
    protection_items = list(protection_status.items()) if isinstance(protection_status, dict) else []
    protection_missing_sl = [symbol for symbol, status in protection_items if isinstance(status, dict) and status.get("missing_sl")]
    protection_missing_tp = [symbol for symbol, status in protection_items if isinstance(status, dict) and status.get("missing_tp")]
    protection_orphan = [
        symbol for symbol, status in protection_items
        if isinstance(status, dict) and int(status.get("orphan_cancelled") or 0) > 0
    ]

    return {
        "events": len(events),
        "candidate_count": len(candidate_keys),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "entry_blocked_count": len(blocked),
        "acceptance_rate_pct": (len(accepted) / max(len(candidate_keys), 1) * 100.0),
        "top_rejects": reject_counts.most_common(5),
        "set_counts": set_counts.most_common(10),
        "top_set": top_set,
        "top_set_share_pct": top_set_share,
        "side_counts": side_counts.most_common(),
        "candidate_type_counts": candidate_type_counts.most_common(),
        "avg_risk_usdt": _avg(event.get("risk_usdt") for event in events),
        "avg_risk_distance_pct": _avg(risk_distance_pct_values),
        "avg_planned_margin": _avg(event.get("planned_margin") for event in events),
        "avg_planned_notional": _avg(event.get("planned_notional") or event.get("target_notional") for event in events),
        "avg_take_profit_pct": _avg(event.get("take_profit_pct") for event in events),
        "avg_slippage_pct": _avg(event.get("entry_slippage_pct") for event in events),
        "funding_events": sum(1 for event in events if event.get("funding_rate") is not None),
        "protection_missing_sl": protection_missing_sl,
        "protection_missing_tp": protection_missing_tp,
        "protection_orphan_cancelled": protection_orphan,
    }


def _format_pairs(pairs, empty="none"):
    if not pairs:
        return empty
    return ", ".join(f"{key}:{value}" for key, value in pairs)


def _fmt(value, digits=2, empty="n/a"):
    value = _safe_float(value)
    if value is None:
        return empty
    return f"{value:.{digits}f}"


def format_research_summary(events, protection_status=None, days=7):
    summary = summarize_diagnostic_events(events, protection_status=protection_status)
    dominance_note = "OK"
    if summary["top_set_share_pct"] >= 50.0:
        dominance_note = "주의: 특정 Set 쏠림 가능"
    lines = [
        "UT Breakout Research Summary",
        f"Window: last {int(days or 7)} days",
        f"Events: {summary['events']} / candidates: {summary['candidate_count']} / accepted: {summary['accepted_count']} / rejected: {summary['rejected_count']} / blocked: {summary['entry_blocked_count']}",
        f"Acceptance: {_fmt(summary['acceptance_rate_pct'], 2)}%",
        f"Top rejects: {_format_pairs(summary['top_rejects'])}",
        f"Set distribution: {_format_pairs(summary['set_counts'])}",
        f"Top set share: {summary['top_set'] or 'none'} {_fmt(summary['top_set_share_pct'], 1)}% ({dominance_note})",
        f"Side mix: {_format_pairs(summary['side_counts'])}",
        f"Candidate types: {_format_pairs(summary['candidate_type_counts'])}",
        f"Avg risk: {_fmt(summary['avg_risk_usdt'], 2)} USDT / stop distance {_fmt(summary['avg_risk_distance_pct'], 3)}% / TP distance {_fmt(summary['avg_take_profit_pct'], 3)}%",
        f"Avg planned: margin {_fmt(summary['avg_planned_margin'], 2)} USDT / notional {_fmt(summary['avg_planned_notional'], 2)} USDT",
        f"Avg slippage: {_fmt(summary['avg_slippage_pct'], 4)}% / funding samples: {summary['funding_events']}",
        f"Protection missing SL: {', '.join(summary['protection_missing_sl']) or 'none'}",
        f"Protection missing TP: {', '.join(summary['protection_missing_tp']) or 'none'}",
        f"Orphan protection cleaned: {', '.join(summary['protection_orphan_cancelled']) or 'none'}",
    ]
    return "\n".join(lines)

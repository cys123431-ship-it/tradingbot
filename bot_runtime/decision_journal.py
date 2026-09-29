"""Append-only JSONL journal of strategy decisions and runtime behaviour.

The daily analysis report reads this journal to explain *why* the bot entered,
skipped or exited a position and whether each safeguard behaved as intended.
Journal I/O must never affect trading: every public function swallows errors.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
JOURNAL_DIR_ENV = "TRADINGBOT_DECISION_JOURNAL_DIR"
JOURNAL_RETENTION_DAYS = 14
STRATEGY = "strategy"
OPERATIONS = "operations"

_ROOT = Path(__file__).resolve().parents[1]
_LOCK = threading.Lock()
_SECRET_PATTERNS = (
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"\b0x[0-9a-fA-F]{64}\b"),  # private keys
    re.compile(r"\b[A-Za-z0-9]{48,}\b"),  # exchange API keys / secrets
)
_SECRET_KEYS = re.compile(r"(api[_-]?key|secret|token|password|private[_-]?key|jwt)", re.I)


def redact_text(text):
    value = str(text)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def journal_dir():
    configured = os.getenv(JOURNAL_DIR_ENV)
    if configured:
        return Path(configured)
    if os.getenv("TRADINGBOT_OFFICIAL_LAUNCHER") == "1":
        return _ROOT / "runtime" / "decision_journal"
    return None


def _clean(value, depth=0):
    """Return a JSON-safe, secret-free, size-bounded copy of ``value``."""
    if depth > 4:
        return str(value)[:200]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return redact_text(value[:2000])
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                out["_truncated_keys"] = len(value) - 80
                break
            key_text = str(key)
            if _SECRET_KEYS.search(key_text):
                out[key_text] = "[REDACTED]"
            else:
                out[key_text] = _clean(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        cleaned = [_clean(item, depth + 1) for item in items[:60]]
        if len(items) > 60:
            cleaned.append(f"...+{len(items) - 60} more")
        return cleaned
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return redact_text(str(value)[:500])


def compact_detail(detail, limit=60):
    """Keep scalar indicator values from a strategy detail dict."""
    if not isinstance(detail, dict):
        return {}
    out = {}
    for key, value in detail.items():
        if len(out) >= limit:
            break
        if isinstance(value, (str, bool, int, float)) or value is None:
            out[str(key)] = value
    return _clean(out)


def _file_for(moment):
    return f"{moment.astimezone(KST):%Y-%m-%d}.jsonl"


def journal_event(category, event, **fields):
    """Append one event.  Never raises."""
    try:
        directory = journal_dir()
        if directory is None:
            return False
        now = datetime.now(timezone.utc)
        record = {
            "ts": now.isoformat(),
            "kst": now.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "category": str(category),
            "event": str(event),
        }
        record.update(_clean(fields))
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / _file_for(now), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return True
    except Exception:
        logger.debug("decision journal write failed", exc_info=True)
        return False


def read_journal(start, end, directory=None):
    """Return events with start <= ts < end (aware datetimes)."""
    base = Path(directory) if directory else journal_dir()
    if base is None or not base.exists():
        return []
    events = []
    day = start.astimezone(KST).date() - timedelta(days=1)
    last = end.astimezone(KST).date()
    while day <= last:
        path = base / f"{day:%Y-%m-%d}.jsonl"
        day += timedelta(days=1)
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        item = json.loads(raw)
                        moment = datetime.fromisoformat(str(item.get("ts")))
                    except (TypeError, ValueError):
                        continue
                    if start <= moment < end:
                        events.append(item)
        except OSError:
            logger.debug("decision journal read failed: %s", path, exc_info=True)
    events.sort(key=lambda item: str(item.get("ts")))
    return events


def prune_journal(now=None, directory=None):
    """Delete journal files older than the retention window.  Never raises."""
    try:
        base = Path(directory) if directory else journal_dir()
        if base is None or not base.exists():
            return 0
        cutoff = (now or datetime.now(timezone.utc)).astimezone(KST).date() - timedelta(
            days=JOURNAL_RETENTION_DAYS
        )
        removed = 0
        for path in base.glob("*.jsonl"):
            try:
                file_day = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_day < cutoff:
                path.unlink()
                removed += 1
        return removed
    except Exception:
        logger.debug("decision journal prune failed", exc_info=True)
        return 0


__all__ = (
    "OPERATIONS",
    "STRATEGY",
    "compact_detail",
    "journal_dir",
    "journal_event",
    "prune_journal",
    "read_journal",
    "redact_text",
)

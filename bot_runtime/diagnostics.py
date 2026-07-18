"""Persistent diagnostic logging and summaries."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
import numpy as np
from utbreakout.research import format_research_summary
logger = logging.getLogger(__name__)

RUNTIME_DIR = os.environ.get('TRADINGBOT_RUNTIME_DIR', 'runtime')
UTBREAKOUT_DIAGNOSTIC_LOG_FILE = os.path.abspath(
    os.environ.get(
        'UTBREAKOUT_DIAGNOSTIC_LOG_FILE',
        os.path.join(RUNTIME_DIR, 'utbreakout_diagnostics.log'),
    )
)
UTBREAKOUT_DIAGNOSTIC_MAX_BYTES = 5 * 1024 * 1024
UTBREAKOUT_DIAGNOSTIC_BACKUP_COUNT = 2
PREDICTION_PAPER_LEDGER_FILE = os.path.abspath(
    os.environ.get(
        'PREDICTION_PAPER_LEDGER_FILE',
        os.path.join(RUNTIME_DIR, 'prediction_paper_ledger.json'),
    )
)
PREDICTION_DIAGNOSTIC_LOG_FILE = os.path.abspath(
    os.environ.get(
        'PREDICTION_DIAGNOSTIC_LOG_FILE',
        os.path.join(RUNTIME_DIR, 'prediction_micro_diagnostics.log'),
    )
)
PREDICTION_DIAGNOSTIC_MAX_BYTES = 3 * 1024 * 1024
PREDICTION_DIAGNOSTIC_BACKUP_COUNT = 2


def _build_utbreakout_diagnostic_logger():
    diag_logger = logging.getLogger('utbreakout_diagnostics')
    diag_logger.setLevel(logging.INFO)
    diag_logger.propagate = False
    if not any(isinstance(h, RotatingFileHandler) for h in diag_logger.handlers):
        os.makedirs(os.path.dirname(UTBREAKOUT_DIAGNOSTIC_LOG_FILE), exist_ok=True)
        handler = RotatingFileHandler(
            UTBREAKOUT_DIAGNOSTIC_LOG_FILE,
            maxBytes=UTBREAKOUT_DIAGNOSTIC_MAX_BYTES,
            backupCount=UTBREAKOUT_DIAGNOSTIC_BACKUP_COUNT,
            encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter('%(message)s'))
        diag_logger.addHandler(handler)
    return diag_logger


utbreakout_diag_logger = _build_utbreakout_diagnostic_logger()


def _build_prediction_diagnostic_logger():
    diag_logger = logging.getLogger('prediction_micro_diagnostics')
    diag_logger.setLevel(logging.INFO)
    diag_logger.propagate = False
    if not any(isinstance(h, RotatingFileHandler) for h in diag_logger.handlers):
        os.makedirs(os.path.dirname(PREDICTION_DIAGNOSTIC_LOG_FILE), exist_ok=True)
        handler = RotatingFileHandler(
            PREDICTION_DIAGNOSTIC_LOG_FILE,
            maxBytes=PREDICTION_DIAGNOSTIC_MAX_BYTES,
            backupCount=PREDICTION_DIAGNOSTIC_BACKUP_COUNT,
            encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter('%(message)s'))
        diag_logger.addHandler(handler)
    return diag_logger


prediction_diag_logger = _build_prediction_diagnostic_logger()


def log_prediction_diagnostic_event(event):
    try:
        payload = dict(event or {})
        payload.setdefault('ts', datetime.now(timezone.utc).isoformat())
        prediction_diag_logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception as e:
        logger.debug(f"Prediction diagnostic event write failed: {e}")


def read_prediction_diagnostic_events(days=7):
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 7)))
    paths = []
    for idx in range(PREDICTION_DIAGNOSTIC_BACKUP_COUNT, 0, -1):
        rotated = f"{PREDICTION_DIAGNOSTIC_LOG_FILE}.{idx}"
        if os.path.exists(rotated):
            paths.append(rotated)
    if os.path.exists(PREDICTION_DIAGNOSTIC_LOG_FILE):
        paths.append(PREDICTION_DIAGNOSTIC_LOG_FILE)

    events = []
    for path in paths:
        try:
            with open(path, 'r', encoding='utf-8') as fp:
                for line in fp:
                    try:
                        event = json.loads(line.strip())
                    except (TypeError, json.JSONDecodeError):
                        continue
                    raw_ts = event.get('ts')
                    try:
                        event_ts = datetime.fromisoformat(str(raw_ts).replace('Z', '+00:00'))
                    except (TypeError, ValueError):
                        continue
                    if event_ts.tzinfo is None:
                        event_ts = event_ts.replace(tzinfo=timezone.utc)
                    event_ts = event_ts.astimezone(timezone.utc)
                    if event_ts >= cutoff:
                        events.append(event)
        except OSError:
            continue
    return events


def _safe_float_or_none(value):
    try:
        if value is None or value == '':
            return None
        parsed = float(value)
        return parsed if np.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def get_utbreakout_diagnostic_log_paths():
    paths = []
    for idx in range(UTBREAKOUT_DIAGNOSTIC_BACKUP_COUNT, 0, -1):
        rotated = f"{UTBREAKOUT_DIAGNOSTIC_LOG_FILE}.{idx}"
        if os.path.exists(rotated):
            paths.append(rotated)
    if os.path.exists(UTBREAKOUT_DIAGNOSTIC_LOG_FILE):
        paths.append(UTBREAKOUT_DIAGNOSTIC_LOG_FILE)
    return paths


def read_utbreakout_diagnostic_events(days=7):
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 7)))
    events = []
    for path in get_utbreakout_diagnostic_log_paths():
        try:
            with open(path, 'r', encoding='utf-8') as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    raw_ts = event.get('ts')
                    try:
                        event_ts = datetime.fromisoformat(str(raw_ts).replace('Z', '+00:00'))
                    except (TypeError, ValueError):
                        continue
                    if event_ts.tzinfo is None:
                        event_ts = event_ts.replace(tzinfo=timezone.utc)
                    event_ts = event_ts.astimezone(timezone.utc)
                    if event_ts >= cutoff:
                        event['_dt'] = event_ts
                        events.append(event)
        except OSError:
            continue
    events.sort(key=lambda item: item.get('_dt') or datetime.min.replace(tzinfo=timezone.utc))
    return events


def format_utbreakout_diagnostic_summary():
    events = read_utbreakout_diagnostic_events(days=7)
    if not events:
        return "최근 7일 진단 로그 없음"

    now = datetime.now(timezone.utc)
    lines = []
    for label, hours in (('24h', 24), ('7d', 24 * 7)):
        cutoff = now - timedelta(hours=hours)
        window_events = [e for e in events if e.get('_dt') and e['_dt'] >= cutoff]
        candidate_keys = {
            (
                e.get('symbol'),
                e.get('side'),
                e.get('decision_candle_ts') or e.get('ut_signal_ts') or e.get('ts')
            )
            for e in window_events
        }
        accepted = sum(1 for e in window_events if e.get('code') == 'ACCEPTED_ENTRY')
        blocked = sum(1 for e in window_events if e.get('event') == 'entry_blocked')
        rejected = [e for e in window_events if str(e.get('code') or '').startswith('REJECTED_')]
        code_counts = Counter(e.get('code') or 'UNKNOWN' for e in rejected)
        top_codes = ', '.join(f"{code}:{count}" for code, count in code_counts.most_common(3)) or 'none'
        lines.append(
            f"{label}: candidates {len(candidate_keys)}, accepted {accepted}, "
            f"blocked {blocked}, top rejects {top_codes}"
        )

    last = events[-1]
    last_dt = last.get('_dt')
    if last_dt:
        kst = last_dt.astimezone(timezone(timedelta(hours=9))).strftime('%m-%d %H:%M')
    else:
        kst = 'unknown'
    lines.append(
        f"last: {kst} {last.get('symbol', '?')} {str(last.get('side') or '?').upper()} "
        f"{last.get('code') or last.get('event') or 'UNKNOWN'}"
    )
    return "\n".join(lines)


def format_utbreakout_research_summary(protection_status=None, days=7):
    events = read_utbreakout_diagnostic_events(days=days)
    if not events:
        return (
            "UT Breakout Research Summary\n"
            f"Window: last {int(days or 7)} days\n"
            "No diagnostic events yet. 다음 15분 판단봉 이후 다시 확인하세요."
        )
    return format_research_summary(
        events,
        protection_status=protection_status or {},
        days=days
    )

__all__ = (
    'RUNTIME_DIR',
    'UTBREAKOUT_DIAGNOSTIC_LOG_FILE',
    'UTBREAKOUT_DIAGNOSTIC_MAX_BYTES',
    'UTBREAKOUT_DIAGNOSTIC_BACKUP_COUNT',
    'PREDICTION_PAPER_LEDGER_FILE',
    'PREDICTION_DIAGNOSTIC_LOG_FILE',
    'PREDICTION_DIAGNOSTIC_MAX_BYTES',
    'PREDICTION_DIAGNOSTIC_BACKUP_COUNT',
    '_build_utbreakout_diagnostic_logger',
    'utbreakout_diag_logger',
    '_build_prediction_diagnostic_logger',
    'prediction_diag_logger',
    'log_prediction_diagnostic_event',
    'read_prediction_diagnostic_events',
    '_safe_float_or_none',
    'get_utbreakout_diagnostic_log_paths',
    'read_utbreakout_diagnostic_events',
    'format_utbreakout_diagnostic_summary',
    'format_utbreakout_research_summary',
)

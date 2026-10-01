"""Daily (09:00 KST -> next 09:00 KST) strategy + runtime analysis report.

The report is written for an AI reviewer.  PART 1 explains every trade and
every entry/exit decision of the window; PART 2 verifies that the automatic
trading code behaved as designed and lists invariant violations.  Sources are
the durable trade DB, the order-state store and audit log, the decision
journal (``decision_journal.py``) and the bot log files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .decision_journal import read_journal, redact_text

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
REPORT_START_HOUR_KST = 9
EMA200_STRATEGY = "ema200_utbot_rsi_2h"
MAX_REPORT_BYTES = 20 * 1024 * 1024
MAX_JOURNAL_APPENDIX_BYTES = 6 * 1024 * 1024
MAX_MARKET_REPLAY_TRADES = 40
_ROOT = Path(__file__).resolve().parents[1]
_SYMBOL_TOKEN = re.compile(r"\b[A-Z0-9]{2,20}/[A-Z]{3,5}(?::[A-Z]{3,5})?")
_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - (\w+) - (.*)$"
)

# Where each behaviour lives, so an AI reviewer can open the right code.
CODE_REFERENCES = {
    "EMA200 scan / candidate ranking": "bot_runtime/signal_scanner.py:_scan_and_trade_ema200_volume, bot_runtime/ema200_candidate_selector.py",
    "EMA200 entry signal (EMA200/UT/RSI, selectable 1h-12h)": "bot_runtime/signal_ema200_utbot_rsi.py:_calculate_ema200_utbot_rsi_signal, bot_runtime/ema200_utbot_rsi.py:evaluate_ema200_utbot_rsi_entry",
    "Exit-timeframe UT alignment entry gate": "bot_runtime/signal_ema200_utbot_rsi.py:_ema200_exit_timeframe_aligned",
    "24h volume universe gate": "bot_runtime/signal_scanner.py:_ema200_entry_volume_allowed",
    "Loss limits / loss streak / margin ladder": "bot_runtime/ema200_utbot_rsi.py:evaluate_ema200_utbot_rsi_loss_gate, get_ema200_consecutive_losses, build_ema200_utbot_rsi_risk_plan",
    "Order submission / fill confirmation": "bot_runtime/signal_entry.py:SignalEntryMixin.entry, trading_safety/order_gateway.py",
    "Emergency SL / protection audit": "bot_runtime/signal_protection.py:_place_tp_sl_orders, _audit_protection_orders",
    "Profit-protection stop ratchet": "bot_runtime/signal_scanner.py:_ema200_apply_margin_profit_stop, bot_runtime/ema200_profit_stop.py",
    "Mechanical UT exit": "bot_runtime/signal_candles.py:process_exit_candle (EMA200 branch), bot_runtime/signal_breakout_analysis.py:_calculate_utbot_signal",
    "Poll loop / exit fallback": "bot_runtime/signal_scanner.py:poll_tick, poll_symbol, _poll_symbol_exit_fallback",
    "Trade accounting / PnL": "trading_safety/trade_accounting.py:record_closed_trade_accounting, bot_runtime/database.py",
    "Restart reconciliation": "trading_safety/reconciliation.py, bot_runtime/signal_runtime.py",
    "Morning entry reset": "bot_runtime/ema200_session.py:perform_ema200_morning_entry_reset",
}


# --------------------------------------------------------------- time window
def _aware(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def report_window(now=None, *, previous=False):
    """Return (start_utc, end_utc, complete) for the 09:00 KST trading day."""
    current = _aware(now) or datetime.now(timezone.utc)
    local = current.astimezone(KST)
    anchor = local.replace(hour=REPORT_START_HOUR_KST, minute=0, second=0, microsecond=0)
    if local < anchor:
        anchor -= timedelta(days=1)
    if previous:
        return (
            (anchor - timedelta(days=1)).astimezone(timezone.utc),
            anchor.astimezone(timezone.utc),
            True,
        )
    return anchor.astimezone(timezone.utc), current, False


def _kst(value, fmt="%m-%d %H:%M:%S"):
    moment = _aware(value)
    if moment is None and isinstance(value, (int, float)) and value > 1e11:
        moment = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    return moment.astimezone(KST).strftime(fmt) if moment else "-"


def _ms_kst(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).astimezone(KST).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _num(value, digits=4):
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


# -------------------------------------------------------- UT replay (mirror)
def ut_state_series(rows, key_value=1.0, atr_period=10, use_heikin_ashi=False):
    """Mirror of ``_calculate_utbot_signal`` over a full series of candles.

    Returns one item per candle once ATR is available: timestamp, close, trail
    stop, bias and the fresh signal of that candle.  The live bot evaluates a
    rolling 300-candle window, so early values can differ slightly; the report
    labels this as a replay.
    """
    items = []
    prev_close = None
    tr = []
    for row in rows:
        high, low, close = float(row[2]), float(row[3]), float(row[4])
        if prev_close is None:
            tr.append(abs(high - low))
        else:
            tr.append(max(abs(high - low), abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    atr = [None] * len(rows)
    if len(rows) >= atr_period:
        atr[atr_period - 1] = sum(tr[:atr_period]) / atr_period
        for i in range(atr_period, len(rows)):
            atr[i] = (atr[i - 1] * (atr_period - 1) + tr[i]) / atr_period
    trail = None
    prev_src = None
    for i, row in enumerate(rows):
        if atr[i] is None:
            continue
        src = (
            (float(row[1]) + float(row[2]) + float(row[3]) + float(row[4])) / 4.0
            if use_heikin_ashi
            else float(row[4])
        )
        nloss = atr[i] * key_value
        if trail is None:
            new_trail = src - nloss
        elif src > trail and prev_src > trail:
            new_trail = max(trail, src - nloss)
        elif src < trail and prev_src < trail:
            new_trail = min(trail, src + nloss)
        elif src > trail:
            new_trail = src - nloss
        else:
            new_trail = src + nloss
        signal = None
        if trail is not None:
            if src > new_trail and prev_src <= trail:
                signal = "long"
            elif src < new_trail and prev_src >= trail:
                signal = "short"
        items.append({
            "ts": int(row[0]),
            "close": float(row[4]),
            "trail": new_trail,
            "bias": "long" if src > new_trail else "short" if src < new_trail else None,
            "signal": signal,
        })
        trail = new_trail
        prev_src = src
    return items


def excursions(side, entry_price, candles, leverage=1.0):
    """Max favourable / adverse excursion in price % and margin ROE %."""
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or not candles:
        return None
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    if str(side).lower() == "long":
        mfe = (max(highs) / entry - 1.0) * 100.0
        mae = (min(lows) / entry - 1.0) * 100.0
    else:
        mfe = (1.0 - min(lows) / entry) * 100.0
        mae = (1.0 - max(highs) / entry) * 100.0
    lev = float(leverage or 1.0)
    return {
        "mfe_price_pct": mfe,
        "mae_price_pct": mae,
        "mfe_roe_pct": mfe * lev,
        "mae_roe_pct": mae * lev,
        "candles": len(candles),
    }


# ------------------------------------------------------------- log scanning
def default_log_paths():
    """Prefer the persistent rotating trading_bot.log family.

    ``emas.log`` is moved to ``emas.log.prev`` on every deploy, so after two
    deploys a day it no longer covers the report window; trading_bot.log keeps
    rotating across restarts (50MB x 4) and also holds logger.exception
    tracebacks.  emas.log is only used when trading_bot.log is missing.
    """
    paths = []
    for base in (_ROOT / "trading_bot.log", _ROOT / "runtime" / "trading_bot.log"):
        for suffix in (".3", ".2", ".1", ""):
            candidate = Path(str(base) + suffix)
            if candidate.exists():
                paths.append(candidate)
        if paths:
            return paths
    log_file = os.getenv("LOG_FILE") or str(Path.home() / "emas.log")
    for candidate in (log_file + ".prev", log_file):
        if Path(candidate).exists():
            paths.append(Path(candidate))
    return paths


def scan_logs(paths, start, end, *, max_tracebacks=15):
    """Group WARNING+ log lines in the window; keep a few tracebacks."""
    groups = {}
    level_counts = Counter()
    tracebacks = []
    for path in paths:
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            current = None
            collecting = None
            for raw in handle:
                line = raw.rstrip("\n")
                match = _LOG_LINE.match(line)
                if not match:
                    if collecting is not None and len(collecting["lines"]) < 40:
                        collecting["lines"].append(line)
                    continue
                if collecting is not None:
                    if len(tracebacks) < max_tracebacks and len(collecting["lines"]) > 1:
                        tracebacks.append(collecting)
                    collecting = None
                try:
                    moment = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
                except ValueError:
                    continue
                moment = moment.astimezone(timezone.utc)
                if not (start <= moment < end):
                    current = None
                    continue
                level, message = match.group(2).upper(), match.group(3)
                level_counts[level] += 1
                current = moment
                if level not in {"WARNING", "ERROR", "CRITICAL"}:
                    continue
                template = re.sub(
                    r"\d+(?:\.\d+)?",
                    "#",
                    _SYMBOL_TOKEN.sub("<SYMBOL>", message),
                )[:180]
                group = groups.setdefault((level, template), {
                    "level": level, "template": template, "count": 0,
                    "first": moment.isoformat(), "last": None, "sample": message[:500],
                })
                group["count"] += 1
                group["last"] = moment.isoformat()
                if level in {"ERROR", "CRITICAL"}:
                    collecting = {"ts": moment.isoformat(), "lines": [message[:500]]}
            if collecting is not None and len(tracebacks) < max_tracebacks and len(collecting["lines"]) > 1:
                tracebacks.append(collecting)
    ordered = sorted(
        groups.values(),
        key=lambda g: ({"CRITICAL": 0, "ERROR": 1, "WARNING": 2}[g["level"]], -g["count"]),
    )
    return {
        "files": [str(p) for p in paths],
        "level_counts": dict(level_counts),
        "groups": ordered,
        "tracebacks": tracebacks,
    }


# ---------------------------------------------------------------- collection
def _trades_in_window(db, start, end):
    with db.lock:
        rows = db.conn.execute(
            """SELECT id, symbol, side, entry_price, exit_price, quantity,
                      pnl_usdt, pnl_pct, entry_time, exit_time, exit_reason,
                      strategy, reconciliation_archived_at
               FROM trades
               WHERE (julianday(entry_time) >= julianday(?) AND julianday(entry_time) < julianday(?))
                  OR (julianday(exit_time) >= julianday(?) AND julianday(exit_time) < julianday(?))
                  OR (exit_time IS NULL AND reconciliation_archived_at IS NULL)
               ORDER BY entry_time""",
            (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
        ).fetchall()
    keys = ("id", "symbol", "side", "entry_price", "exit_price", "quantity", "pnl_usdt",
            "pnl_pct", "entry_time", "exit_time", "exit_reason", "strategy", "archived_at")
    return [dict(zip(keys, row)) for row in rows]


def _git_revision():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(_ROOT),
            capture_output=True, text=True, timeout=3,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _record_summary(record):
    return {
        "client_order_id": record.client_order_id,
        "symbol": record.symbol,
        "side": record.side,
        "strategy": record.strategy,
        "intent": record.order_intent,
        "purpose": record.order_purpose,
        "state": record.order_state,
        "filled_qty": record.filled_qty,
        "avg_fill": record.average_fill_price,
        "stop_order_id": record.stop_order_id,
        "tp_order_ids": list(record.take_profit_order_ids or []),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_error": record.last_error,
        "metadata": {
            key: value for key, value in (record.metadata or {}).items()
            if not isinstance(value, (dict, list)) or key in {"entry_plan_summary"}
        },
    }


async def _market_replay(ctrl, trade, exit_tf, ut_params, end):
    exchange = getattr(ctrl, "market_data_exchange", None)
    entry_at = _aware(trade.get("entry_time"))
    if exchange is None or entry_at is None:
        return None
    exit_at = _aware(trade.get("exit_time")) or end
    tf_ms = {
        "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
        "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
        "12h": 43_200_000, "1d": 86_400_000,
    }.get(exit_tf, 900_000)
    since = int(entry_at.timestamp() * 1000) - 320 * tf_ms
    limit = min(1500, int((exit_at - entry_at).total_seconds() * 1000 / tf_ms) + 330)
    try:
        rows = await asyncio.to_thread(exchange.fetch_ohlcv, trade["symbol"], exit_tf, since=since, limit=limit)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    rows = [r for r in rows or [] if r and len(r) >= 5]
    entry_ms = int(entry_at.timestamp() * 1000)
    exit_ms = int(exit_at.timestamp() * 1000)
    # Excursions use 1m candles inside the actual hold; whole exit-tf candles
    # would include price action from before the entry.
    minute_since = entry_ms - (entry_ms % 60_000)
    minutes = int((exit_ms - minute_since) / 60_000) + 2
    try:
        minute_rows = await asyncio.to_thread(
            exchange.fetch_ohlcv, trade["symbol"], "1m",
            since=minute_since, limit=max(2, min(1500, minutes)),
        )
    except Exception:
        minute_rows = []
    held = [r for r in minute_rows or [] if r and r[0] + 60_000 > entry_ms and r[0] <= exit_ms]
    excursion_basis = "1m"
    if not held:
        held = [r for r in rows if r[0] + tf_ms > entry_ms and r[0] <= exit_ms]
        excursion_basis = exit_tf
    states = await asyncio.to_thread(
        ut_state_series, rows,
        ut_params.get("key_value", 1.0), ut_params.get("atr_period", 10),
        ut_params.get("use_heikin_ashi", False),
    )
    at_entry = next((s for s in reversed(states) if s["ts"] + tf_ms <= entry_ms), None)
    during = [s for s in states if s["ts"] + tf_ms > entry_ms and s["ts"] + tf_ms <= exit_ms]
    return {
        "exit_tf": exit_tf,
        "ut_bias_at_entry": at_entry["bias"] if at_entry else None,
        "ut_signals_during_hold": [
            {"closed_at_kst": _ms_kst(s["ts"] + tf_ms), "signal": s["signal"], "close": s["close"]}
            for s in during if s["signal"]
        ],
        "ut_bias_timeline": [
            {"closed_at_kst": _ms_kst(s["ts"] + tf_ms), "bias": s["bias"], "close": s["close"], "trail": round(s["trail"], 8)}
            for s in during
        ][-200:],
        "excursions": excursions(trade.get("side"), trade.get("entry_price"), held,
                                 leverage=trade.get("_leverage") or 1.0),
        "excursion_basis": excursion_basis,
        "excursion_truncated": bool(minutes > 1500),
    }


async def collect_daily_report_inputs(ctrl, start, end, *, market_replay=True, log_paths=None):
    engines = getattr(ctrl, "engines", {}) or {}
    engine = engines.get("signal")
    store = getattr(engine, "trading_state_store", None) or getattr(ctrl, "trading_state_store", None)
    inputs = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "revision": await asyncio.to_thread(_git_revision),
        "errors": [],
    }

    def attempt(name, func, default):
        try:
            return func()
        except Exception as exc:
            inputs["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            return default

    inputs["trades"] = attempt("trades", lambda: _trades_in_window(ctrl.db, start, end), [])
    inputs["config"] = attempt(
        "config",
        lambda: dict(ctrl._ema200_utbot_rsi_config()) if hasattr(ctrl, "_ema200_utbot_rsi_config") else {},
        {},
    )
    inputs["active_strategy"] = attempt(
        "active_strategy",
        lambda: str(
            ctrl.cfg.get(ctrl.get_active_trade_section(), {}).get("strategy_params", {}).get("active_strategy", "")
        ),
        "",
    )
    inputs["paused"] = bool(getattr(ctrl, "is_paused", False))
    if store is not None:
        inputs["trade_results"] = attempt(
            "trade_results",
            lambda: [
                item for item in store.load_trade_results()
                if isinstance(item, dict) and (_aware(item.get("exit_time")) or start) >= start
                and (_aware(item.get("exit_time")) or start) < end
            ],
            [],
        )
        inputs["order_records"] = attempt(
            "order_records",
            lambda: [_record_summary(r) for r in store.records_touched_between(start.isoformat(), end.isoformat())],
            [],
        )
        inputs["audit"] = attempt("audit", lambda: store.audit_between(start.isoformat(), end.isoformat()), [])
        inputs["runtime_state"] = attempt(
            "runtime_state",
            lambda: {
                key: store.get_runtime_state(key)
                for key in (
                    "entry_lock_reason", "daily_loss_entry_lock",
                    "ema200_morning_entry_state_reset", "ema200_utbot_rsi_daily_loss_reset",
                    "ema200_utbot_rsi_consecutive_loss_reset",
                )
            },
            {},
        )
    else:
        inputs["errors"].append("trading state store unavailable")
        inputs.update(trade_results=[], order_records=[], audit=[], runtime_state={})
    inputs["journal"] = attempt("journal", lambda: read_journal(start, end), [])
    paths = log_paths if log_paths is not None else default_log_paths()
    inputs["logs"] = await asyncio.to_thread(scan_logs, paths, start, end)

    positions = []
    inputs["positions_known"] = False
    exchange = getattr(ctrl, "exchange", None)
    if exchange is not None:
        try:
            for pos in await asyncio.to_thread(exchange.fetch_positions) or []:
                if abs(float(pos.get("contracts", 0.0) or 0.0)) > 0:
                    positions.append({
                        "symbol": pos.get("symbol"), "side": pos.get("side"),
                        "contracts": pos.get("contracts"), "entryPrice": pos.get("entryPrice"),
                        "markPrice": pos.get("markPrice"), "unrealizedPnl": pos.get("unrealizedPnl"),
                        "leverage": pos.get("leverage"), "liquidationPrice": pos.get("liquidationPrice"),
                    })
            inputs["positions_known"] = True
        except Exception as exc:
            inputs["errors"].append(f"positions: {type(exc).__name__}: {exc}")
    inputs["positions"] = positions
    balance_reader = getattr(engine, "get_balance_info", None) if engine else None
    inputs["equity"] = None
    if callable(balance_reader):
        try:
            total, free, _ = await balance_reader()
            inputs["equity"] = {"total": float(total or 0.0), "free": float(free or 0.0)}
        except Exception as exc:
            inputs["errors"].append(f"balance: {type(exc).__name__}: {exc}")

    inputs["market_replay"] = {}
    if market_replay:
        exit_tf = str(inputs["config"].get("exit_timeframe") or "entry")
        if exit_tf not in ("15m", "30m", "1h"):
            exit_tf = str(inputs["config"].get("timeframe") or "2h")
        ut_params = {
            "key_value": float(inputs["config"].get("utbot_key_value", 1.0) or 1.0),
            "atr_period": int(inputs["config"].get("utbot_atr_period", 10) or 10),
            "use_heikin_ashi": bool(inputs["config"].get("utbot_use_heikin_ashi", False)),
        }
        for trade in [t for t in inputs["trades"] if str(t.get("strategy") or "").lower() == EMA200_STRATEGY][:MAX_MARKET_REPLAY_TRADES]:
            trade["_leverage"] = inputs["config"].get("small_account_leverage") or inputs["config"].get("leverage") or 1
            replay = await _market_replay(ctrl, trade, exit_tf, ut_params, end)
            if replay is not None:
                inputs["market_replay"][str(trade["id"])] = replay
    return inputs


# ------------------------------------------------------------------ analysis
def _events(journal, event, symbol=None):
    return [
        e for e in journal
        if e.get("event") == event and (symbol is None or e.get("symbol") == symbol)
    ]


def _between(events, start, end):
    return [e for e in events if start <= (_aware(e.get("ts")) or start) < end]


def consistency_checks(inputs):
    """Return [(severity, code, message)] for invariant violations."""
    journal = inputs.get("journal") or []
    findings = []
    plans = _events(journal, "entry_plan")
    protections = _events(journal, "entry_protection")
    for plan in plans:
        risk = plan.get("risk_plan") or {}
        moment = _aware(plan.get("ts"))
        after = [p for p in protections if p.get("symbol") == plan.get("symbol")
                 and moment and (_aware(p.get("ts")) or moment) >= moment
                 and (_aware(p.get("ts")) or moment) - moment < timedelta(minutes=10)]
        if not after:
            continue
        outcome = after[0].get("outcome")
        if risk.get("emergency_stop_required") and outcome != "PROTECTED":
            findings.append(("CRITICAL", "STOP_REQUIRED_NOT_PROTECTED",
                             f"{plan.get('symbol')} 연속손실 {risk.get('consecutive_losses')}회 단계인데 보호 결과 {outcome}"))
    for item in protections:
        if item.get("outcome") == "STRATEGY_MANAGED_NO_STOP" and int(item.get("streak") or 0) > 0:
            findings.append(("CRITICAL", "NO_STOP_WITH_LOSS_STREAK",
                             f"{item.get('symbol')} 연속손실 {item.get('streak')}회인데 SL 없는 첫 단계로 처리"))
        if item.get("outcome") == "FILLED_UNPROTECTED":
            findings.append(("CRITICAL", "FILLED_UNPROTECTED", f"{item.get('symbol')} 진입 후 SL 미확인 (신규진입 잠금)"))
    fills = _events(journal, "entry_filled")
    for gate in _events(journal, "entry_gate_exit_tf"):
        if gate.get("allowed"):
            continue
        moment = _aware(gate.get("ts"))
        for fill in fills:
            fill_at = _aware(fill.get("ts"))
            if fill.get("symbol") == gate.get("symbol") and moment and fill_at and timedelta(0) <= fill_at - moment < timedelta(minutes=5):
                findings.append(("CRITICAL", "EXIT_TF_GATE_BYPASSED",
                                 f"{gate.get('symbol')} 청산봉 UT 반대로 차단됐는데 체결 기록 존재"))
    streaks = defaultdict(int)
    flagged = set()
    for check in _events(journal, "exit_check"):
        key = (check.get("symbol"), check.get("side"))
        opposite = (check.get("ut_bias") or "") not in ("", None) and check.get("ut_bias") != check.get("side")
        if check.get("decision") == "HOLD" and opposite:
            streaks[key] += 1
            if streaks[key] >= 4 and key not in flagged:
                flagged.add(key)
                findings.append(("WARNING", "OPPOSITE_UT_WITHOUT_FRESH_SIGNAL",
                                 f"{key[0]} {key[1]}: 청산봉 UT가 {streaks[key]}봉 연속 반대인데 fresh 신호가 없어 보유 유지"))
        else:
            streaks[key] = 0
    for name, code in (("poll_exit_fallback", "POLL_EXIT_FALLBACK"), ("profit_stop_error", "PROFIT_STOP_ERROR")):
        count = len(_events(journal, name))
        if count:
            findings.append(("WARNING", code, f"{name} {count}회 발생 (루프 앞 단계 오류)"))
    failed = [row for row in inputs.get("audit") or [] if row.get("new_state") == "FAILED"]
    closed_ids = {
        row.get("client_order_id") for row in inputs.get("audit") or []
        if row.get("new_state") == "CLOSED"
    }
    duplicate_close = [
        row for row in failed
        if "-2022" in str((row.get("detail") or {}).get("last_error"))
        and row.get("client_order_id") in closed_ids
    ]
    if duplicate_close:
        findings.append(("WARNING", "DUPLICATE_REDUCE_ONLY_CLOSE",
                         f"청산 체결 후 같은 청산 주문이 다시 제출돼 reduce-only 거절 {len(duplicate_close)}건 "
                         f"({', '.join(sorted({str(r.get('symbol')) for r in duplicate_close}))}); 최종 CLOSED라 손실 없음, 중복 제출 경로 점검 필요"))
    for row in failed:
        if row in duplicate_close:
            continue
        findings.append(("WARNING", "ORDER_FAILED",
                         f"{row.get('symbol')} {row.get('client_order_id')}: {(row.get('detail') or {}).get('last_error')}"))
    for record in inputs.get("order_records") or []:
        if record.get("state") in {"FILLED_UNPROTECTED", "SUBMITTED_UNKNOWN", "EMERGENCY_CLOSE_FAILED", "FILLED_LIQUIDATION_CONFLICT"}:
            findings.append(("CRITICAL", f"ORDER_STATE_{record.get('state')}",
                             f"{record.get('symbol')} {record.get('client_order_id')} 상태 {record.get('state')}"))
    lock = (inputs.get("runtime_state") or {}).get("entry_lock_reason")
    if lock:
        findings.append(("WARNING", "ENTRY_LOCK_ACTIVE", f"신규진입 잠금 유지 중: {lock}"))
    open_db = {t["symbol"] for t in inputs.get("trades") or [] if not t.get("exit_time") and not t.get("archived_at")}
    open_ex = {p.get("symbol") for p in inputs.get("positions") or []}
    if inputs.get("positions_known"):
        for symbol in sorted(open_db - open_ex):
            findings.append(("WARNING", "DB_OPEN_NOT_ON_EXCHANGE", f"{symbol}: DB에는 미청산인데 거래소 포지션 없음"))
        for symbol in sorted(open_ex - open_db):
            findings.append(("WARNING", "EXCHANGE_POSITION_NOT_IN_DB", f"{symbol}: 거래소 포지션이 DB 미청산 거래에 없음"))
    provisional = [r for r in inputs.get("trade_results") or [] if r.get("provisional")]
    if provisional:
        findings.append(("INFO", "PROVISIONAL_ACCOUNTING", f"수수료·펀딩 미확정 결과 {len(provisional)}건"))
    logs = inputs.get("logs") or {}
    serious = [g for g in logs.get("groups") or [] if g["level"] in {"ERROR", "CRITICAL"}]
    if serious:
        findings.append(("WARNING", "LOG_ERRORS",
                         f"ERROR/CRITICAL 로그 {sum(g['count'] for g in serious)}건 ({len(serious)}종)"))
    for item in inputs.get("errors") or []:
        findings.append(("INFO", "REPORT_DATA_GAP", item))
    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    return sorted(findings, key=lambda f: order.get(f[0], 3))


# ------------------------------------------------------------------ rendering
def _trade_result(trade, inputs):
    """Fee/funding-aware accounting row for a DB trade, if recorded."""
    return next(
        (
            r for r in inputs.get("trade_results") or []
            if r.get("symbol") == trade.get("symbol")
            and str(r.get("entry_time")) == str(trade.get("entry_time"))
        ),
        None,
    )


def _trade_net_pnl(trade, inputs):
    """Return (pnl, basis): net after fees/funding when known, else gross.

    The loss streak uses the same fee-aware net result, so a small gross
    profit eaten by fees is a loss here exactly as it is for the bot.
    """
    result = _trade_result(trade, inputs)
    if result and result.get("net_pnl_usdt") is not None:
        return float(result["net_pnl_usdt"]), "net"
    return float(trade.get("pnl_usdt") or 0.0), "gross"


def _trade_block(index, trade, inputs):
    journal = inputs.get("journal") or []
    symbol = trade.get("symbol")
    entry_at = _aware(trade.get("entry_time"))
    exit_at = _aware(trade.get("exit_time"))
    end = _aware(inputs.get("window_end"))
    lines = []
    status = "보유 중" if not trade.get("exit_time") else "청산"
    lines.append(f"[거래 #{index}] {symbol} {str(trade.get('side') or '').upper()} ({trade.get('strategy') or '-'}) — {status}")
    lines.append(f"  진입: {_kst(entry_at)} KST @ {_num(trade.get('entry_price'), 6)} / 수량 {trade.get('quantity')}")
    near = (entry_at - timedelta(minutes=10), entry_at + timedelta(minutes=10)) if entry_at else None
    if near:
        plan = next(iter(_between(_events(journal, "entry_plan", symbol), *near)), None)
        if plan:
            risk = plan.get("risk_plan") or {}
            entry_plan = plan.get("entry_plan") or {}
            lines.append(
                f"  리스크 계획: 연속손실 {risk.get('consecutive_losses')}회 → 증거금 {risk.get('margin_percent')}% / "
                f"{risk.get('leverage')}x / 비상SL {'필수 ' + str(risk.get('emergency_exit_percent')) + '%' if risk.get('emergency_stop_required') else '없음(첫 단계)'} / "
                f"명목 {_num(plan.get('target_notional'), 2)} USDT, 증거금 {_num(plan.get('margin_to_use'), 2)} USDT, equity {_num(plan.get('sizing_equity'), 2)}"
            )
            if entry_plan:
                lines.append(f"  진입 계획: {json.dumps(entry_plan, ensure_ascii=False, default=str)[:600]}")
        gate = next(iter(_between(_events(journal, "entry_gate_exit_tf", symbol), *near)), None)
        if gate:
            lines.append(f"  청산봉 UT 정렬 게이트: {'통과' if gate.get('allowed') else '차단'} — {gate.get('reason')}")
        scan = next((e for e in reversed(_events(journal, "ema200_scan"))
                     if entry_at - timedelta(hours=2, minutes=10) <= (_aware(e.get("ts")) or entry_at) <= entry_at + timedelta(minutes=5)
                     and any(c.get("symbol") == symbol for c in e.get("candidates") or [])), None)
        if scan:
            candidate = next(c for c in scan.get("candidates") or [] if c.get("symbol") == symbol)
            lines.append(
                f"  후보 선정: 순위 {candidate.get('rank')}/{candidate.get('candidate_count')} 점수 {_num(candidate.get('score'), 2)} "
                f"(UT 경과 {candidate.get('ut_age_bars')}봉, RSI 모멘텀 {candidate.get('rsi_momentum')}, EMA 기울기 {candidate.get('ema_slope_percent')}%, "
                f"확장 {candidate.get('extension_atr')}ATR, 24h 거래대금 {candidate.get('quote_volume_24h')}, 보조UT {candidate.get('auxiliary_ut_biases')})"
            )
            lines.append(f"  점수 구성: {json.dumps(candidate.get('score_breakdown'), ensure_ascii=False, default=str)[:500]}")
            evaluation = next((e for e in scan.get("evaluations") or [] if e.get("symbol") == symbol), None)
            if evaluation:
                lines.append(f"  2h 신호 지표값: {json.dumps(evaluation.get('detail'), ensure_ascii=False, default=str)[:900]}")
        fill = next(iter(_between(_events(journal, "entry_filled", symbol), *near)), None)
        if fill:
            lines.append(
                f"  체결: 요청 {_num(fill.get('requested_price'), 6)} → 체결 {_num(fill.get('fill_price'), 6)} "
                f"(order {fill.get('order_id')}, client {fill.get('client_order_id')}, 확인 {fill.get('confirmation_source')})"
            )
        protection = next(iter(_between(_events(journal, "entry_protection", symbol), *near)), None)
        if protection:
            lines.append(f"  보호주문 결과: {protection.get('outcome')} (stop {protection.get('stop_order_id')})")
    hold_end = exit_at or end
    if entry_at and hold_end:
        checks = _between(_events(journal, "exit_check", symbol), entry_at, hold_end + timedelta(minutes=1))
        if checks:
            decisions = Counter(c.get("decision") for c in checks)
            lines.append(f"  청산 검사 {len(checks)}회 ({dict(decisions)}) — 청산봉 {checks[-1].get('exit_tf')}")
            for check in checks[-24:]:
                lines.append(
                    f"    · {_ms_kst((check.get('closed_candle_ts') or 0))} 봉: UT상태 {check.get('ut_bias')} / fresh {check.get('ut_fresh_signal') or '-'} "
                    f"/ stop {_num(check.get('ut_stop'), 6)} / src {_num(check.get('ut_src'), 6)} → {check.get('decision')}"
                    + (" (진입 전 신호 무시)" if check.get("pre_entry_signal_ignored") else "")
                )
        for status_event in _between(_events(journal, "profit_stop_status", symbol), entry_at, hold_end + timedelta(minutes=1)):
            lines.append(f"    · 수익Stop {_kst(status_event.get('ts'))}: {status_event.get('previous_status')} → {status_event.get('status')} {json.dumps(status_event.get('details'), ensure_ascii=False, default=str)[:300]}")
        for executed in _between(_events(journal, "exit_executed", symbol), entry_at, hold_end + timedelta(minutes=1)):
            lines.append(f"  청산 실행: {_kst(executed.get('ts'))} {executed.get('reason')} (잔여 포지션 {executed.get('remaining_position')})")
    if trade.get("exit_time"):
        result = _trade_result(trade, inputs)
        hold = (exit_at - entry_at) if entry_at and exit_at else None
        net, basis = _trade_net_pnl(trade, inputs)
        outcome = "이익" if net > 0 else "손실" if net < 0 else "본전"
        lines.append(
            f"  청산: {_kst(exit_at)} KST @ {_num(trade.get('exit_price'), 6)} / 사유 {trade.get('exit_reason')} / "
            f"결과 {outcome} (순손익 {_num(net)} USDT, {'수수료·펀딩 포함' if basis == 'net' else '수수료 미확정, 가격손익'}) / "
            f"가격손익 {_num(trade.get('pnl_usdt'))} USDT ({_num(trade.get('pnl_pct'), 2)}%) / 보유 {str(hold).split('.')[0] if hold else '-'}"
        )
        if basis == "net" and (float(trade.get("pnl_usdt") or 0.0) > 0) != (net > 0):
            lines.append(
                "  ※ 가격손익과 순손익의 부호가 다릅니다. 연속손실 단계는 순손익 기준이라 "
                f"이 거래는 {'손실' if net < 0 else '이익/본전'}으로 집계됩니다."
            )
        if result:
            lines.append(
                f"  정산: net {_num(result.get('net_pnl_usdt'))} / gross {_num(result.get('gross_pnl_usdt'))} / "
                f"수수료 {_num(result.get('entry_fee_usdt'))}+{_num(result.get('exit_fee_usdt'))} / 펀딩 {_num(result.get('funding_usdt'))} / "
                f"{'확정' if not result.get('provisional') else '미확정'} / 청산 레그 {json.dumps(result.get('exit_legs'), ensure_ascii=False, default=str)[:400]}"
            )
    replay = (inputs.get("market_replay") or {}).get(str(trade.get("id")))
    if replay:
        if replay.get("error"):
            lines.append(f"  시장 재생 실패: {replay['error']}")
        else:
            exc = replay.get("excursions") or {}
            lines.append(
                f"  시장 재생({replay.get('exit_tf')} UT 재계산): 진입 시 UT {replay.get('ut_bias_at_entry')} / "
                f"MFE {_num(exc.get('mfe_price_pct'), 2)}% (ROE {_num(exc.get('mfe_roe_pct'), 2)}%) / "
                f"MAE {_num(exc.get('mae_price_pct'), 2)}% (ROE {_num(exc.get('mae_roe_pct'), 2)}%) "
                f"[{replay.get('excursion_basis')}봉 기준{', 앞 1500분만' if replay.get('excursion_truncated') else ''}]"
            )
            signals = replay.get("ut_signals_during_hold") or []
            lines.append(
                "  보유 중 UT fresh 신호: "
                + (", ".join(f"{s['closed_at_kst']} {s['signal']}@{s['close']}" for s in signals[:20]) or "없음")
            )
    return lines


def build_daily_analysis_report(inputs):
    start = _aware(inputs.get("window_start"))
    end = _aware(inputs.get("window_end"))
    journal = inputs.get("journal") or []
    trades = inputs.get("trades") or []
    lines = []
    lines.append("=" * 78)
    lines.append("자동매매 일일 분석 리포트 (AI 분석용)")
    lines.append(f"기간: {_kst(start, '%Y-%m-%d %H:%M')} ~ {_kst(end, '%Y-%m-%d %H:%M')} KST")
    lines.append(f"생성: {_kst(inputs.get('generated_at'), '%Y-%m-%d %H:%M:%S')} KST / 코드 revision {inputs.get('revision')}")
    lines.append("=" * 78)
    lines.append("")
    lines.append("[읽는 법] PART 1은 매매전략 분석(언제·왜 진입/미진입/청산했는지), PART 2는 코드 작동 검증")
    lines.append("(각 기능이 설계대로 동작했는지와 불변식 위반)입니다. 각 항목의 code_ref와 [코드 위치]로")
    lines.append("해당 로직을 찾을 수 있습니다. 시각은 모두 KST, 가격 단위는 USDT입니다.")
    lines.append("")
    lines.append("[전략 설정 스냅샷]")
    lines.append(f"  활성 전략: {inputs.get('active_strategy')} / 일시정지: {inputs.get('paused')} / equity: {json.dumps(inputs.get('equity'))}")
    lines.append(f"  EMA200 설정: {json.dumps(inputs.get('config'), ensure_ascii=False, default=str)}")
    lines.append("")

    # ----------------------------------------------------------- PART 1
    lines.append("#" * 78)
    lines.append("PART 1. 매매전략 분석")
    lines.append("#" * 78)
    closed = [t for t in trades if t.get("exit_time") and start <= (_aware(t.get("exit_time")) or start) < end]
    pnls = [_trade_net_pnl(t, inputs)[0] for t in closed]
    gross = [float(t.get("pnl_usdt") or 0.0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    entered = [t for t in trades if start <= (_aware(t.get("entry_time")) or start) < end]
    lines.append("[1-1 요약] (승·패와 손익은 수수료·펀딩 포함 순손익 기준 — 봇의 연속손실 판단과 동일)")
    lines.append(f"  기간 내 진입 {len(entered)}건 / 청산 {len(closed)}건 / 승 {len(wins)} 패 {len(losses)} 본전 {len(pnls) - len(wins) - len(losses)}")
    lines.append(
        f"  순손익 {sum(pnls):+.4f} USDT (가격손익 {sum(gross):+.4f}, 수수료·펀딩 {sum(pnls) - sum(gross):+.4f}) / "
        f"평균이익 {(sum(wins) / len(wins)) if wins else 0:+.4f} / 평균손실 {(sum(losses) / len(losses)) if losses else 0:+.4f} / "
        f"Profit Factor {(sum(wins) / abs(sum(losses))) if losses else ('∞' if wins else '-')}"
    )
    by_reason = Counter(str(t.get("exit_reason") or "-") for t in closed)
    lines.append(f"  청산 사유 분포: {dict(by_reason)}")
    lines.append("")
    lines.append("[1-2 거래별 상세]")
    if not trades:
        lines.append("  거래 없음")
    for index, trade in enumerate(trades, 1):
        lines.extend(_trade_block(index, trade, inputs))
        lines.append("")
    lines.append("[1-3 스캔·후보 판단 기록 (2h 완료봉 기준)]")
    scans = _events(journal, "ema200_scan")
    if not scans:
        lines.append("  기록 없음 (판단 기록은 이 기능 배포 이후부터 저장됩니다)")
    for scan in scans:
        candidates = scan.get("candidates") or []
        selected = (scan.get("selected") or {}).get("symbol")
        lines.append(
            f"  · {_kst(scan.get('ts'))} 완료봉 {_ms_kst(scan.get('closed_candle_ts'))}: 평가 {scan.get('evaluated_symbols')}종목, "
            f"후보 {len(candidates)} → 선택 {selected or '-'} / {scan.get('reason')}"
        )
        for candidate in candidates[:5]:
            lines.append(
                f"      후보 {candidate.get('rank')}. {candidate.get('symbol')} {str(candidate.get('side') or '').upper()} 점수 {_num(candidate.get('score'), 2)} "
                f"UT경과 {candidate.get('ut_age_bars')} RSI모멘텀 {candidate.get('rsi_momentum')} 보조UT {candidate.get('auxiliary_ut_biases')}"
            )
    lines.append("")
    lines.append("[1-4 진입 차단·보류 기록]")
    blocks = _events(journal, "entry_blocked") + [g for g in _events(journal, "entry_gate_exit_tf") if not g.get("allowed")]
    blocks.sort(key=lambda e: str(e.get("ts")))
    lines.append(f"  사유별: {dict(Counter(e.get('gate') or 'exit_tf_alignment' for e in blocks))}")
    for block in blocks[-60:]:
        lines.append(f"  · {_kst(block.get('ts'))} {block.get('symbol')} {str(block.get('side') or '').upper()} [{block.get('gate') or 'exit_tf_alignment'}] {block.get('reason')}")
    lines.append("")

    # ----------------------------------------------------------- PART 2
    lines.append("#" * 78)
    lines.append("PART 2. 자동매매 코드 작동 검증")
    lines.append("#" * 78)
    findings = consistency_checks(inputs)
    lines.append("[2-1 자동 불변식 점검]")
    if not findings:
        lines.append("  이상 없음")
    for severity, code, message in findings:
        lines.append(f"  [{severity}] {code}: {message}")
    lines.append("")
    lines.append("[2-2 기능별 작동 기록]")
    counts = Counter(e.get("event") for e in journal)
    lines.append(f"  판단 기록 이벤트 수: {dict(counts)}")
    for event in journal:
        if event.get("category") != "operations":
            continue
        payload = {k: v for k, v in event.items() if k not in {"ts", "kst", "category", "event", "code_ref"}}
        lines.append(f"  · {event.get('kst')} {event.get('event')} @ {event.get('code_ref')}: {json.dumps(payload, ensure_ascii=False, default=str)[:700]}")
    lines.append("")
    lines.append("[2-3 주문 상태 전이 (order_state_audit)]")
    audit = inputs.get("audit") or []
    if not audit:
        lines.append("  기록 없음")
    for row in audit[-300:]:
        lines.append(
            f"  · {_kst(row.get('timestamp'))} {row.get('symbol')} {row.get('client_order_id')}: {row.get('old_state')} → {row.get('new_state')} "
            f"{json.dumps(row.get('detail'), ensure_ascii=False, default=str)[:300]}"
        )
    lines.append("")
    lines.append("[2-4 기간 중 변경된 주문 기록 (crypto_orders)]")
    for record in inputs.get("order_records") or []:
        lines.append(f"  · {json.dumps(record, ensure_ascii=False, default=str)[:900]}")
    lines.append("")
    lines.append("[2-5 런타임 상태]")
    lines.append(f"  {json.dumps(inputs.get('runtime_state'), ensure_ascii=False, default=str)}")
    lines.append(f"  현재 거래소 포지션: {json.dumps(inputs.get('positions'), ensure_ascii=False, default=str)}")
    lines.append("")
    logs = inputs.get("logs") or {}
    lines.append("[2-6 로그 경고·오류 (같은 형태는 묶어서 집계)]")
    lines.append(f"  파일: {logs.get('files')} / 레벨별 {logs.get('level_counts')}")
    for group in (logs.get("groups") or [])[:80]:
        lines.append(f"  · [{group['level']}] x{group['count']} ({_kst(group['first'])}~{_kst(group['last'])}) {group['sample'][:400]}")
    for trace in (logs.get("tracebacks") or []):
        lines.append(f"  -- traceback {_kst(trace['ts'])} --")
        lines.extend(f"     {line[:300]}" for line in trace["lines"])
    lines.append("")
    lines.append("[2-7 코드 위치]")
    for name, ref in CODE_REFERENCES.items():
        lines.append(f"  · {name}: {ref}")
    lines.append("")

    # ---------------------------------------------------------- APPENDIX
    lines.append("#" * 78)
    lines.append("APPENDIX. 원본 데이터 (JSON, AI 정밀 분석용)")
    lines.append("#" * 78)
    lines.append("[A-1 거래(DB)] " + json.dumps(trades, ensure_ascii=False, default=str))
    lines.append("[A-2 정산 결과] " + json.dumps(inputs.get("trade_results"), ensure_ascii=False, default=str)[:400000])
    lines.append("[A-3 시장 재생] " + json.dumps(inputs.get("market_replay"), ensure_ascii=False, default=str)[:800000])
    lines.append("[A-4 판단 기록 원본 (JSONL)]")
    used = 0
    for event in journal:
        raw = json.dumps(event, ensure_ascii=False, default=str)
        used += len(raw.encode("utf-8"))
        if used > MAX_JOURNAL_APPENDIX_BYTES:
            lines.append(f"... 크기 제한으로 이후 {len(journal)} 중 나머지 생략")
            break
        lines.append(raw)
    text = redact_text("\n".join(lines))
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        text = encoded[:MAX_REPORT_BYTES].decode("utf-8", errors="ignore") + "\n... (리포트 크기 제한으로 잘림)"
    return text


def report_filename(start, end, complete):
    return (
        f"daily_analysis_{_kst(start, '%Y%m%d_%H%M')}"
        f"_to_{_kst(end, '%Y%m%d_%H%M')}{'' if complete else '_partial'}.txt"
    )


__all__ = (
    "CODE_REFERENCES",
    "build_daily_analysis_report",
    "collect_daily_report_inputs",
    "consistency_checks",
    "excursions",
    "report_filename",
    "report_window",
    "scan_logs",
    "ut_state_series",
)

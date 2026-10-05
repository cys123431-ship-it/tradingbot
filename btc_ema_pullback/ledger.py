"""Trade records, structured event log, daily limits and performance statistics.

Uses its own SQLite file (separate from the main bot's trade DB) so this
strategy's trades never change the main strategies' statistics or loss-streak
sizing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import STRATEGY_VERSION

logger = logging.getLogger("btc_ema_pullback")

TRADE_COLUMNS = (
    "trade_id", "signal_id", "mode", "network", "status", "entry_time", "exit_time",
    "side", "quantity", "entry_price", "stop_price", "take_profit_price", "exit_price",
    "gross_pnl", "commission", "funding", "net_pnl", "pnl_pct", "r_multiple", "initial_risk",
    "result", "exit_reason", "rule_violation", "strategy_version", "extra",
)


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def utc_now():
    return datetime.now(timezone.utc)


def trading_day_bounds(now=None, tz_name="Asia/Seoul"):
    """(start_utc, end_utc, day_key) of the trading day containing ``now``."""
    tz = ZoneInfo(tz_name)
    local = (now or utc_now()).astimezone(tz)
    start_local = datetime.combine(local.date(), dtime(0, 0), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), local.date().isoformat()


class StrategyLedger:
    def __init__(self, db_path, events_path):
        self.db_path = Path(db_path)
        self.events_path = Path(events_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS trades ("
                + ", ".join(f"{c} TEXT" if c != "trade_id" else "trade_id TEXT PRIMARY KEY" for c in TRADE_COLUMNS)
                + ")"
            )
            self._conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")

    def close(self):
        with self._lock:
            self._conn.close()

    # ----- key/value state (state machine, processed signals, day snapshot) -----
    def get_state(self, key, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set_state(self, key, value):
        payload = json.dumps(value, default=_json_default, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, payload),
            )

    # ----- structured events -----
    def event(self, kind, /, **fields):
        record = {"ts": utc_now().isoformat(), "event": kind, **fields}
        line = json.dumps(record, default=_json_default, ensure_ascii=False)
        logger.info("BTC_PULLBACK %s %s", kind, line)
        try:
            with self._lock, open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            logger.exception("BTC pullback event log write failed")
        return record

    def recent_events(self, limit=20, kinds=None):
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()[-2000:]
        except OSError:
            return []
        rows = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if kinds and row.get("event") not in kinds:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    # ----- trades -----
    def upsert_trade(self, trade):
        row = {key: trade.get(key) for key in TRADE_COLUMNS}
        row["strategy_version"] = row.get("strategy_version") or STRATEGY_VERSION
        for key, value in list(row.items()):
            if isinstance(value, (dict, list)):
                row[key] = json.dumps(value, default=_json_default, ensure_ascii=False)
            elif value is not None and not isinstance(value, str):
                row[key] = str(value)
        columns = ", ".join(TRADE_COLUMNS)
        marks = ", ".join("?" for _ in TRADE_COLUMNS)
        updates = ", ".join(f"{c}=COALESCE(excluded.{c}, trades.{c})" for c in TRADE_COLUMNS if c != "trade_id")
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO trades({columns}) VALUES({marks}) ON CONFLICT(trade_id) DO UPDATE SET {updates}",
                tuple(row[c] for c in TRADE_COLUMNS),
            )

    def get_trade(self, trade_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def trades(self, *, mode=None, network=None, since=None, until=None, status=None):
        query, args = "SELECT * FROM trades WHERE 1=1", []
        for column, value in (("mode", mode), ("network", network), ("status", status)):
            if value is not None:
                query += f" AND {column}=?"
                args.append(value)
        if since is not None:
            query += " AND entry_time>=?"
            args.append(since.isoformat())
        if until is not None:
            query += " AND entry_time<?"
            args.append(until.isoformat())
        query += " ORDER BY entry_time"
        with self._lock:
            return [dict(r) for r in self._conn.execute(query, args).fetchall()]


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def daily_entry_block_reason(trades_today, cfg, *, day_start_equity, current_equity):
    """Why new entries are blocked for the rest of the trading day (or '')."""
    count = len(trades_today)
    if count >= int(cfg["max_trades_per_day"]):
        return f"DAILY_TRADE_LIMIT: {count}/{cfg['max_trades_per_day']} trades today"
    streak = 0
    for trade in reversed([t for t in trades_today if t.get("status") == "CLOSED"]):
        if _num(trade.get("net_pnl")) < 0:
            streak += 1
        else:
            break
    if streak >= int(cfg["max_consecutive_losses"]):
        return f"CONSECUTIVE_LOSS_LIMIT: {streak} losses in a row today"
    start, current = _num(day_start_equity), _num(current_equity)
    if start > 0 and current > 0:
        change = (current - start) / start
        if change <= -float(cfg["max_daily_loss_pct"]):
            return f"DAILY_LOSS_LIMIT: equity {change * 100:.2f}% vs day start (limit -{float(cfg['max_daily_loss_pct']) * 100:.2f}%)"
    return ""


def compute_stats(trades):
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    pnls = [_num(t.get("net_pnl")) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rs = [_num(t.get("r_multiple")) for t in closed if t.get("r_multiple") not in (None, "")]
    max_w = max_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif p < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        max_w, max_l = max(max_w, cur_w), max(max_l, cur_l)
    equity = peak = drawdown = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    gross_profit, gross_loss = sum(wins), sum(losses)
    total = len(closed)
    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / total) if total else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": sum(pnls),
        "average_win": (gross_profit / len(wins)) if wins else 0.0,
        "average_loss": (gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_profit / abs(gross_loss)) if gross_loss else (float("inf") if gross_profit else 0.0),
        "expectancy": (sum(pnls) / total) if total else 0.0,
        "average_r": (sum(rs) / len(rs)) if rs else 0.0,
        "max_consecutive_wins": max_w,
        "max_consecutive_losses": max_l,
        "max_drawdown": drawdown,
    }


__all__ = (
    "StrategyLedger",
    "compute_stats",
    "daily_entry_block_reason",
    "trading_day_bounds",
)

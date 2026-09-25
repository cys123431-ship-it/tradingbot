"""SQLite trade and status persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
import sqlite3
import threading
from zoneinfo import ZoneInfo


KST = ZoneInfo('Asia/Seoul')


def _kst_day_bounds_utc(now=None):
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    local_now = reference.astimezone(KST)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
    )


def _timestamp_in_range(value, start, end):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return start <= parsed < end

class DBManager:
    def __init__(self, db_path='bot_database.db'):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self.lock:
            self.conn.execute("""CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY, symbol TEXT, side TEXT,
                entry_price REAL, exit_price REAL, quantity REAL,
                pnl_usdt REAL, pnl_pct REAL,
                entry_time TEXT, exit_time TEXT, exit_reason TEXT,
                strategy TEXT
            )""")
            trade_columns = {
                str(row[1])
                for row in self.conn.execute("PRAGMA table_info(trades)").fetchall()
            }
            if 'strategy' not in trade_columns:
                self.conn.execute("ALTER TABLE trades ADD COLUMN strategy TEXT")
            if 'reconciliation_archived_at' not in trade_columns:
                self.conn.execute(
                    "ALTER TABLE trades ADD COLUMN reconciliation_archived_at TEXT"
                )
            if 'reconciliation_archive_reason' not in trade_columns:
                self.conn.execute(
                    "ALTER TABLE trades ADD COLUMN reconciliation_archive_reason TEXT"
                )
            self.conn.execute("""CREATE TABLE IF NOT EXISTS shannon_log (
                id INTEGER PRIMARY KEY, timestamp TEXT,
                total_equity REAL, action TEXT,
                coin_price REAL, coin_amt REAL, usdt_amt REAL
            )""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS grid_orders (
                id INTEGER PRIMARY KEY, symbol TEXT, side TEXT,
                price REAL, quantity REAL, order_id TEXT,
                status TEXT, created_at TEXT
            )""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS status_history (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                snapshot_key TEXT,
                snapshot_text TEXT
            )""")
            self.conn.commit()

    def log_shannon(self, equity, action, price, coin, usdt):
        with self.lock:
            self.conn.execute(
                "INSERT INTO shannon_log (timestamp, total_equity, action, coin_price, coin_amt, usdt_amt) VALUES (?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), equity, action, price, coin, usdt)
            )
            self.conn.commit()

    def log_trade_entry(self, symbol, side, price, quantity=0, strategy=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO trades (symbol, side, entry_price, quantity, entry_time, strategy) VALUES (?,?,?,?,?,?)",
                (
                    symbol,
                    side,
                    price,
                    quantity,
                    datetime.now(timezone.utc).isoformat(),
                    str(strategy or '').strip().lower() or None,
                )
            )
            self.conn.commit()

    def log_trade_close(
        self,
        symbol,
        pnl,
        pnl_pct,
        exit_price,
        reason,
        *,
        exit_time=None,
        quantity=None,
    ):
        resolved_exit_time = (
            str(exit_time).strip()
            if exit_time not in (None, '')
            else datetime.now(timezone.utc).isoformat()
        )
        with self.lock:
            cur = self.conn.execute(
                """UPDATE trades SET exit_time=?, exit_price=?, pnl_usdt=?, pnl_pct=?, exit_reason=?,
                quantity=COALESCE(?, quantity)
                WHERE id=(
                    SELECT id FROM trades
                    WHERE symbol=? AND exit_time IS NULL
                      AND reconciliation_archived_at IS NULL
                    ORDER BY id DESC LIMIT 1
                )""",
                (
                    resolved_exit_time,
                    exit_price,
                    pnl,
                    pnl_pct,
                    reason,
                    quantity,
                    symbol,
                )
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def get_daily_stats(self):
        start, end = _kst_day_bounds_utc()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT COUNT(*), SUM(pnl_usdt) FROM trades
                WHERE julianday(exit_time) >= julianday(?)
                  AND julianday(exit_time) < julianday(?)""",
                (start.isoformat(), end.isoformat()),
            )
            res = cur.fetchone()
            legacy_count = res[0] if res and res[0] else 0
            legacy_pnl = res[1] if res and res[1] else 0.0
        store = getattr(self, 'trade_result_store', None)
        loader = getattr(store, 'load_trade_results', None)
        if callable(loader):
            try:
                results = [
                    item
                    for item in loader() or []
                    if isinstance(item, dict)
                    and _timestamp_in_range(item.get('exit_time'), start, end)
                ]
                # Use fee/funding-aware results only when they cover the full
                # legacy daily ledger. A partial accounting outage must not
                # silently omit trades from the risk limit.
                if results and len(results) >= legacy_count:
                    return len(results), sum(
                        float(
                            item.get('net_pnl_usdt')
                            if item.get('net_pnl_usdt') is not None
                            else item.get('gross_pnl_usdt')
                            or 0.0
                        )
                        for item in results
                    )
            except Exception:
                pass
        return legacy_count, legacy_pnl

    def get_weekly_stats(self):
        _, today_end = _kst_day_bounds_utc()
        week_ago = today_end - timedelta(days=7)
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT COUNT(*), SUM(pnl_usdt) FROM trades
                WHERE julianday(exit_time) >= julianday(?)
                  AND julianday(exit_time) < julianday(?)""",
                (week_ago.isoformat(), today_end.isoformat()),
            )
            res = cur.fetchone()
            return (res[0] if res and res[0] else 0), (res[1] if res and res[1] else 0.0)

    def get_daily_entry_count(self):
        start, end = _kst_day_bounds_utc()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT COUNT(*) FROM trades
                WHERE julianday(entry_time) >= julianday(?)
                  AND julianday(entry_time) < julianday(?)""",
                (start.isoformat(), end.isoformat()),
            )
            res = cur.fetchone()
            return res[0] if res and res[0] else 0

    def get_daily_automatic_entry_count(self):
        """Count Korea-calendar-day entries owned by automatic strategies."""
        start, end = _kst_day_bounds_utc()
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT COUNT(*) FROM trades
                WHERE julianday(entry_time) >= julianday(?)
                  AND julianday(entry_time) < julianday(?)
                  AND LOWER(COALESCE(strategy, '')) NOT IN ('user_custom', 'custom_entry')""",
                (start.isoformat(), end.isoformat()),
            )
            res = cur.fetchone()
            return res[0] if res and res[0] else 0

    def get_daily_automatic_symbol_entry(self, symbol, *, now=None):
        """Return today's latest automatic entry for a symbol, if any.

        The calendar boundary is Korea time and the comparison accepts the
        Binance symbol spellings used by CCXT, exchange payloads, and legacy
        database rows (for example ``BTCUSDT`` and ``BTC/USDT:USDT``).
        User-custom entries are deliberately excluded.
        """
        normalized_symbol = (
            str(symbol or "")
            .strip()
            .upper()
            .replace(":USDT", "")
            .replace("/", "")
            .replace("-", "")
        )
        if not normalized_symbol:
            return None

        start, end = _kst_day_bounds_utc(now)
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT symbol, side, entry_time, strategy
                FROM trades
                WHERE julianday(entry_time) >= julianday(?)
                  AND julianday(entry_time) < julianday(?)
                  AND LOWER(COALESCE(strategy, '')) NOT IN ('user_custom', 'custom_entry')
                  AND REPLACE(
                        REPLACE(
                            REPLACE(UPPER(symbol), ':USDT', ''),
                            '/',
                            ''
                        ),
                        '-',
                        ''
                      ) = ?
                ORDER BY entry_time DESC, id DESC
                LIMIT 1""",
                (start.isoformat(), end.isoformat(), normalized_symbol),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "symbol": row[0],
            "side": row[1],
            "entry_time": row[2],
            "strategy": row[3],
        }

    def get_latest_automatic_symbol_trade(self, symbol):
        """Return the newest automatic entry and its confirmed close across days.

        A close that is still being reconciled has no ``exit_time``; callers
        must keep blocking re-entry until that close is recorded.  An archived
        exchange-flat legacy trade uses its confirmed reconciliation time.
        """
        normalized_symbol = (
            str(symbol or "").strip().upper()
            .replace(":USDT", "").replace("/", "").replace("-", "")
        )
        if not normalized_symbol:
            return None
        with self.lock:
            row = self.conn.execute(
                """SELECT symbol, side, entry_time, exit_time,
                          reconciliation_archived_at, strategy
                FROM trades
                WHERE LOWER(COALESCE(strategy, '')) NOT IN ('user_custom', 'custom_entry')
                  AND REPLACE(REPLACE(REPLACE(UPPER(symbol), ':USDT', ''), '/', ''), '-', '') = ?
                ORDER BY COALESCE(julianday(entry_time), 0) DESC, id DESC LIMIT 1""",
                (normalized_symbol,),
            ).fetchone()
        if row is None:
            return None
        return {
            "symbol": row[0], "side": row[1], "entry_time": row[2],
            "exit_time": row[3], "reconciliation_archived_at": row[4],
            "strategy": row[5],
        }

    def get_recent_closed_trade_pnls(
        self,
        limit=10,
        today_only=False,
        strategies=None,
    ):
        limit = max(1, int(limit or 1))
        if isinstance(strategies, str):
            strategies = [strategies]
        strategy_values = sorted({
            str(value or '').strip().lower()
            for value in (strategies or [])
            if str(value or '').strip()
        })
        where = ["exit_time IS NOT NULL"]
        params = []
        if today_only:
            start, end = _kst_day_bounds_utc()
            where.extend((
                "julianday(exit_time) >= julianday(?)",
                "julianday(exit_time) < julianday(?)",
            ))
            params.extend((start.isoformat(), end.isoformat()))
        if strategy_values:
            placeholders = ','.join('?' for _ in strategy_values)
            where.append(f"LOWER(COALESCE(strategy, '')) IN ({placeholders})")
            params.extend(strategy_values)
        params.append(limit)
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT pnl_usdt FROM trades WHERE "
                + " AND ".join(where)
                + " ORDER BY exit_time DESC, id DESC LIMIT ?",
                tuple(params),
            )
            return [float(row[0] or 0.0) for row in cur.fetchall()]

    def get_consecutive_strategy_losses(self, strategy, limit=100, *, since=None):
        """Return the newest-first realized-loss streak for one strategy.

        This query is intentionally strategy-attributed and durable so a bot
        restart cannot reset the EMA200 small-account risk stage. Break-even
        and profitable closes reset the streak.
        """
        strategy_value = str(strategy or '').strip().lower()
        if not strategy_value:
            raise ValueError('strategy is required')
        limit = max(1, min(1000, int(limit or 100)))
        since_utc = None
        if since not in (None, ''):
            try:
                since_utc = datetime.fromisoformat(
                    str(since).replace('Z', '+00:00')
                )
            except (TypeError, ValueError) as exc:
                raise ValueError('invalid consecutive-loss cutoff') from exc
            if since_utc.tzinfo is None:
                raise ValueError('consecutive-loss cutoff must be timezone-aware')
            since_utc = since_utc.astimezone(timezone.utc)

        where = [
            "exit_time IS NOT NULL",
            "pnl_usdt IS NOT NULL",
            "LOWER(COALESCE(strategy, '')) = ?",
        ]
        params = [strategy_value]
        if since_utc is not None:
            where.append("julianday(exit_time) > julianday(?)")
            params.append(since_utc.isoformat())
        params.append(limit)
        with self.lock:
            rows = self.conn.execute(
                "SELECT pnl_usdt, exit_time FROM trades WHERE "
                + " AND ".join(where)
                + " ORDER BY exit_time DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()

        pnl_values = [float(row[0]) for row in rows]
        store = getattr(self, 'trade_result_store', None)
        loader = getattr(store, 'load_trade_results', None)
        if callable(loader):
            try:
                results = []
                for item in loader() or []:
                    if not isinstance(item, dict) or not item.get('exit_time'):
                        continue
                    if since_utc is not None:
                        try:
                            result_exit = datetime.fromisoformat(
                                str(item['exit_time']).replace('Z', '+00:00')
                            )
                        except (TypeError, ValueError) as exc:
                            raise ValueError(
                                'invalid exit timestamp in trade result store'
                            ) from exc
                        if result_exit.tzinfo is None:
                            result_exit = result_exit.replace(tzinfo=timezone.utc)
                        if result_exit.astimezone(timezone.utc) <= since_utc:
                            continue
                    attributed = {
                        str(item.get(key) or '').strip().lower()
                        for key in (
                            'primary_strategy',
                            'selected_strategy',
                            'strategy',
                        )
                        if str(item.get(key) or '').strip()
                    }
                    if strategy_value not in attributed:
                        continue
                    raw_pnl = (
                        item.get('net_pnl_usdt')
                        if item.get('net_pnl_usdt') is not None
                        else item.get('gross_pnl_usdt')
                    )
                    pnl = float(raw_pnl)
                    if not isfinite(pnl):
                        raise ValueError(
                            'non-finite realized PnL in trade result store'
                        )
                    results.append((str(item['exit_time']), pnl))
                results.sort(key=lambda item: item[0], reverse=True)
                # Only prefer fee/funding-aware accounting when it covers the
                # full queried ledger. A partial accounting outage must not
                # silently reset an existing loss streak.
                if results and len(results) >= len(rows):
                    pnl_values = [pnl for _, pnl in results[:limit]]
            except Exception:
                if not rows:
                    raise

        streak = 0
        for pnl in pnl_values:
            if not isfinite(pnl):
                raise ValueError('non-finite realized PnL in trade history')
            if pnl < 0:
                streak += 1
                continue
            break
        return streak

    def get_latest_open_trade(self, symbol):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT symbol, side, entry_price, quantity, entry_time, strategy
                FROM trades WHERE symbol=? AND exit_time IS NULL
                  AND reconciliation_archived_at IS NULL
                ORDER BY id DESC LIMIT 1""",
                (symbol,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                'symbol': row[0],
                'side': row[1],
                'entry_price': float(row[2] or 0.0),
                'quantity': float(row[3] or 0.0),
                'entry_time': row[4],
                'strategy': row[5],
            }

    def get_open_trades(self):
        """Return every locally open trade for exchange-flat reconciliation."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT symbol, side, entry_price, quantity, entry_time, strategy
                FROM trades WHERE exit_time IS NULL
                  AND reconciliation_archived_at IS NULL
                ORDER BY id DESC"""
            )
            return [
                {
                    'symbol': row[0],
                    'side': row[1],
                    'entry_price': float(row[2] or 0.0),
                    'quantity': float(row[3] or 0.0),
                    'entry_time': row[4],
                    'strategy': row[5],
                }
                for row in cur.fetchall()
            ]

    def archive_open_trade(self, symbol, entry_time, reason):
        """Archive an exchange-flat legacy row without inventing exit PnL.

        Old databases can contain entries that predate durable order identity
        tracking.  Once a complete exchange snapshot confirms the symbol is
        flat and no matching durable entry record exists, the row must stop
        behaving like a live trade while remaining available for audit.
        """
        archived_at = datetime.now(timezone.utc).isoformat()
        with self.lock:
            cur = self.conn.execute(
                """UPDATE trades
                SET reconciliation_archived_at=?, reconciliation_archive_reason=?
                WHERE id=(
                    SELECT id FROM trades
                    WHERE symbol=? AND entry_time=? AND exit_time IS NULL
                      AND reconciliation_archived_at IS NULL
                    ORDER BY id DESC LIMIT 1
                )""",
                (archived_at, str(reason or ''), symbol, entry_time),
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def log_status_snapshot(self, snapshot_key, snapshot_text, keep_rows=200):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT snapshot_key FROM status_history ORDER BY id DESC LIMIT 1")
            latest = cur.fetchone()
            if latest and latest[0] == snapshot_key:
                return False

            cur.execute(
                "INSERT INTO status_history (created_at, snapshot_key, snapshot_text) VALUES (?,?,?)",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), snapshot_key, snapshot_text)
            )
            keep_rows = max(1, int(keep_rows))
            self.conn.execute(
                f"DELETE FROM status_history WHERE id NOT IN (SELECT id FROM status_history ORDER BY id DESC LIMIT {keep_rows})"
            )
            self.conn.commit()
            return True

    def get_recent_status_history(self, limit=5, offset=0):
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT created_at, snapshot_text FROM status_history ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            return cur.fetchall()

__all__ = (
    'DBManager',
)

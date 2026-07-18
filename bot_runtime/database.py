"""SQLite trade and status persistence."""

from __future__ import annotations

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

    def log_trade_close(self, symbol, pnl, pnl_pct, exit_price, reason):
        with self.lock:
            cur = self.conn.execute(
                """UPDATE trades SET exit_time=?, exit_price=?, pnl_usdt=?, pnl_pct=?, exit_reason=?
                WHERE id=(
                    SELECT id FROM trades
                    WHERE symbol=? AND exit_time IS NULL
                    ORDER BY id DESC LIMIT 1
                )""",
                (datetime.now(timezone.utc).isoformat(), exit_price, pnl, pnl_pct, reason, symbol)
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def get_daily_stats(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*), SUM(pnl_usdt) FROM trades WHERE exit_time LIKE ?", (f"{today}%",))
            res = cur.fetchone()
            return (res[0] if res and res[0] else 0), (res[1] if res and res[1] else 0.0)

    def get_weekly_stats(self):
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*), SUM(pnl_usdt) FROM trades WHERE exit_time >= ?", (week_ago,))
            res = cur.fetchone()
            return (res[0] if res and res[0] else 0), (res[1] if res and res[1] else 0.0)

    def get_daily_entry_count(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades WHERE entry_time LIKE ?", (f"{today}%",))
            res = cur.fetchone()
            return res[0] if res and res[0] else 0

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
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            where.append("exit_time LIKE ?")
            params.append(f"{today}%")
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

    def get_latest_open_trade(self, symbol):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT symbol, side, entry_price, quantity, entry_time
                FROM trades WHERE symbol=? AND exit_time IS NULL
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
            }

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

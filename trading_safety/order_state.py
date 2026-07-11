"""Durable crypto order state and audit storage.

SQLite is used because transactions and uniqueness constraints give stronger
crash and duplicate-event guarantees than a collection of runtime JSON files.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable


logger = logging.getLogger(__name__)


class OrderState(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED_UNKNOWN = "SUBMITTED_UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED_UNVERIFIED_LIQUIDATION = "FILLED_UNVERIFIED_LIQUIDATION"
    FILLED_LIQUIDATION_CONFLICT = "FILLED_LIQUIDATION_CONFLICT"
    FILLED_UNPROTECTED = "FILLED_UNPROTECTED"
    PROTECTED = "PROTECTED"
    CLOSING = "CLOSING"
    EMERGENCY_CLOSE_FAILED = "EMERGENCY_CLOSE_FAILED"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"


ACTIVE_ORDER_STATES = frozenset(
    {
        OrderState.PLANNED.value,
        OrderState.SUBMITTING.value,
        OrderState.SUBMITTED_UNKNOWN.value,
        OrderState.ACKNOWLEDGED.value,
        OrderState.PARTIALLY_FILLED.value,
        OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
        OrderState.FILLED_LIQUIDATION_CONFLICT.value,
        OrderState.FILLED_UNPROTECTED.value,
        OrderState.PROTECTED.value,
        OrderState.CLOSING.value,
        OrderState.EMERGENCY_CLOSE_FAILED.value,
    }
)
ENTRY_BLOCKING_STATES = frozenset(
    {
        OrderState.SUBMITTING.value,
        OrderState.SUBMITTED_UNKNOWN.value,
        OrderState.PARTIALLY_FILLED.value,
        OrderState.FILLED_UNVERIFIED_LIQUIDATION.value,
        OrderState.FILLED_LIQUIDATION_CONFLICT.value,
        OrderState.FILLED_UNPROTECTED.value,
        OrderState.CLOSING.value,
        OrderState.EMERGENCY_CLOSE_FAILED.value,
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> str:
    if isinstance(value, Enum):
        return value.value
    return str(value)


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=_json_default)


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    ensure_ascii: bool = True,
    indent: int | None = 2,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=ensure_ascii,
                indent=indent,
                sort_keys=True,
                default=_json_default,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _normalize_signal_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return str(int(value.timestamp()))
    try:
        number = float(value)
    except (TypeError, ValueError):
        text = str(value or "unknown").strip()
        return re.sub(r"[^A-Za-z0-9]", "", text)[:16] or "unknown"
    if number > 10_000_000_000:
        number /= 1000.0
    return str(int(number))


def _normalize_order_symbol(value: Any) -> str:
    """Normalize CCXT, Binance, and display symbols for state lookups."""

    text = str(value or "").upper().strip()
    if ":" in text:
        text = text.split(":", 1)[0]
    return re.sub(r"[^A-Z0-9]", "", text)


def build_client_order_id(
    strategy: str,
    symbol: str,
    side: str,
    signal_timestamp: Any,
    stage: str = "entry",
    *,
    max_length: int = 36,
) -> str:
    """Return a Binance-safe deterministic client order ID."""

    strategy_key = re.sub(r"[^a-z0-9]", "", str(strategy or "bot").lower())[:5] or "bot"
    symbol_key = re.sub(r"[^a-z0-9]", "", str(symbol or "coin").lower())
    symbol_key = symbol_key.replace("usdtusdt", "usdt")[:11] or "coin"
    side_key = "l" if str(side or "").lower() in {"long", "buy", "l"} else "s"
    stage_key = re.sub(r"[^a-z0-9]", "", str(stage or "entry").lower())[:4] or "ent"
    timestamp_key = _normalize_signal_timestamp(signal_timestamp)
    digest_input = "|".join((strategy_key, symbol_key, side_key, timestamp_key, stage_key))
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:8]
    prefix = f"{strategy_key}-{symbol_key}-{side_key}-{stage_key}"
    result = f"{prefix}-{timestamp_key}-{digest}"
    if len(result) > max_length:
        room = max(1, max_length - len(digest) - 1)
        result = f"{prefix[:room]}-{digest}"
    return re.sub(r"[^A-Za-z0-9_-]", "", result)[:max_length]


@dataclass
class OrderRecord:
    client_order_id: str
    symbol: str
    side: str
    strategy: str
    signal_timestamp: str
    requested_qty: float
    order_state: str = OrderState.PLANNED.value
    exchange_order_id: str | None = None
    filled_qty: float = 0.0
    average_fill_price: float = 0.0
    stop_order_id: str | None = None
    take_profit_order_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_error: str | None = None
    retry_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> "OrderRecord":
        self.order_state = str(getattr(self.order_state, "value", self.order_state))
        self.requested_qty = float(self.requested_qty or 0.0)
        self.filled_qty = float(self.filled_qty or 0.0)
        self.average_fill_price = float(self.average_fill_price or 0.0)
        self.retry_count = int(self.retry_count or 0)
        self.updated_at = self.updated_at or utc_now_iso()
        return self


class SQLiteTradingStateStore:
    """Transactionally stores order states, runtime safety state, and trades."""

    def __init__(self, path: str | Path = "runtime/trading_state.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS crypto_orders (
                    client_order_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    signal_timestamp TEXT NOT NULL,
                    requested_qty REAL NOT NULL,
                    filled_qty REAL NOT NULL DEFAULT 0,
                    average_fill_price REAL NOT NULL DEFAULT 0,
                    order_state TEXT NOT NULL,
                    stop_order_id TEXT,
                    take_profit_order_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_crypto_orders_symbol_state
                    ON crypto_orders(symbol, order_state);
                CREATE TABLE IF NOT EXISTS order_state_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL,
                    old_state TEXT,
                    new_state TEXT NOT NULL,
                    exchange_order_id TEXT,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS runtime_safety_state (
                    state_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_trade_results (
                    trade_id TEXT PRIMARY KEY,
                    client_order_id TEXT,
                    strategy TEXT,
                    engine TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT,
                    exit_time TEXT,
                    payload_json TEXT NOT NULL,
                    provisional INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> OrderRecord | None:
        if row is None:
            return None
        return OrderRecord(
            client_order_id=row["client_order_id"],
            exchange_order_id=row["exchange_order_id"],
            symbol=row["symbol"],
            side=row["side"],
            strategy=row["strategy"],
            signal_timestamp=row["signal_timestamp"],
            requested_qty=row["requested_qty"],
            filled_qty=row["filled_qty"],
            average_fill_price=row["average_fill_price"],
            order_state=row["order_state"],
            stop_order_id=row["stop_order_id"],
            take_profit_order_ids=json.loads(row["take_profit_order_ids_json"] or "[]"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
            retry_count=row["retry_count"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def get(self, client_order_id: str) -> OrderRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM crypto_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return self._row_to_record(row)

    def upsert(self, record: OrderRecord) -> OrderRecord:
        record = record.normalized()
        previous = self.get(record.client_order_id)
        record.updated_at = utc_now_iso()
        if previous and not record.created_at:
            record.created_at = previous.created_at
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO crypto_orders (
                    client_order_id, exchange_order_id, symbol, side, strategy,
                    signal_timestamp, requested_qty, filled_qty, average_fill_price,
                    order_state, stop_order_id, take_profit_order_ids_json, created_at,
                    updated_at, last_error, retry_count, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    exchange_order_id=excluded.exchange_order_id,
                    symbol=excluded.symbol,
                    side=excluded.side,
                    strategy=excluded.strategy,
                    signal_timestamp=excluded.signal_timestamp,
                    requested_qty=excluded.requested_qty,
                    filled_qty=excluded.filled_qty,
                    average_fill_price=excluded.average_fill_price,
                    order_state=excluded.order_state,
                    stop_order_id=excluded.stop_order_id,
                    take_profit_order_ids_json=excluded.take_profit_order_ids_json,
                    updated_at=excluded.updated_at,
                    last_error=excluded.last_error,
                    retry_count=excluded.retry_count,
                    metadata_json=excluded.metadata_json
                """,
                (
                    record.client_order_id,
                    record.exchange_order_id,
                    record.symbol,
                    record.side,
                    record.strategy,
                    str(record.signal_timestamp),
                    record.requested_qty,
                    record.filled_qty,
                    record.average_fill_price,
                    record.order_state,
                    record.stop_order_id,
                    _safe_json(record.take_profit_order_ids),
                    record.created_at,
                    record.updated_at,
                    record.last_error,
                    record.retry_count,
                    _safe_json(record.metadata),
                ),
            )
            if previous is None or previous.order_state != record.order_state:
                self._insert_audit(previous, record, {})
        return record

    def _insert_audit(
        self,
        previous: OrderRecord | None,
        record: OrderRecord,
        detail: dict[str, Any],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO order_state_audit (
                client_order_id, old_state, new_state, exchange_order_id,
                symbol, timestamp, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.client_order_id,
                previous.order_state if previous else None,
                record.order_state,
                record.exchange_order_id,
                record.symbol,
                record.updated_at,
                _safe_json(detail),
            ),
        )
        logger.info(
            _safe_json(
                {
                    "event": "ORDER_STATE_CHANGED",
                    "client_order_id": record.client_order_id,
                    "symbol": record.symbol,
                    "old_state": previous.order_state if previous else None,
                    "new_state": record.order_state,
                    "exchange_order_id": record.exchange_order_id,
                    "timestamp": record.updated_at,
                }
            )
        )

    def transition(self, client_order_id: str, new_state: str | OrderState, **changes: Any) -> OrderRecord:
        record = self.get(client_order_id)
        if record is None:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        old = OrderRecord(**asdict(record))
        state_value = str(getattr(new_state, "value", new_state))
        record.order_state = state_value
        for key, value in changes.items():
            if hasattr(record, key):
                setattr(record, key, value)
            else:
                record.metadata[key] = value
        record.updated_at = utc_now_iso()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE crypto_orders SET
                    exchange_order_id=?, filled_qty=?, average_fill_price=?, order_state=?,
                    stop_order_id=?, take_profit_order_ids_json=?, updated_at=?, last_error=?,
                    retry_count=?, metadata_json=? WHERE client_order_id=?
                """,
                (
                    record.exchange_order_id,
                    record.filled_qty,
                    record.average_fill_price,
                    record.order_state,
                    record.stop_order_id,
                    _safe_json(record.take_profit_order_ids),
                    record.updated_at,
                    record.last_error,
                    record.retry_count,
                    _safe_json(record.metadata),
                    record.client_order_id,
                ),
            )
            if old.order_state != record.order_state:
                self._insert_audit(old, record, changes)
        return record

    def list_by_states(self, states: Iterable[str | OrderState]) -> list[OrderRecord]:
        values = [str(getattr(state, "value", state)) for state in states]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM crypto_orders WHERE order_state IN ({placeholders}) ORDER BY created_at",  # nosec B608
                values,
            ).fetchall()
        return [record for row in rows if (record := self._row_to_record(row)) is not None]

    def records_for_symbol(self, symbol: str) -> list[OrderRecord]:
        normalized = _normalize_order_symbol(symbol)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM crypto_orders ORDER BY created_at"
            ).fetchall()
        return [
            record
            for row in rows
            if (record := self._row_to_record(row)) is not None
            and _normalize_order_symbol(record.symbol) == normalized
        ]

    def active_for_symbol(self, symbol: str) -> list[OrderRecord]:
        normalized = _normalize_order_symbol(symbol)
        return [
            record
            for record in self.list_by_states(ACTIVE_ORDER_STATES)
            if _normalize_order_symbol(record.symbol) == normalized
        ]

    def entry_block_reason(self, symbol: str | None = None) -> str | None:
        records = self.list_by_states(ENTRY_BLOCKING_STATES)
        if not records:
            return None
        same_symbol = [
            record for record in records
            if symbol and _normalize_order_symbol(record.symbol) == _normalize_order_symbol(symbol)
        ]
        selected = same_symbol[0] if same_symbol else records[0]
        return f"{selected.order_state}:{selected.symbol}:{selected.client_order_id}"

    def set_runtime_state(self, key: str, value: Any) -> None:
        now = utc_now_iso()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO runtime_safety_state(state_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, _safe_json(value), now),
            )

    def get_runtime_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM runtime_safety_state WHERE state_key=?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, ValueError):
            logger.exception("Invalid runtime safety state for key=%s", key)
            return default

    def record_trade_result(self, trade: dict[str, Any]) -> bool:
        trade_id = str(trade.get("trade_id") or "").strip()
        if not trade_id:
            raise ValueError("trade_id is required")
        now = utc_now_iso()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO live_trade_results (
                    trade_id, client_order_id, strategy, engine, symbol, side,
                    exit_time, payload_json, provisional, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    trade.get("client_order_id"),
                    trade.get("strategy"),
                    trade.get("engine"),
                    str(trade.get("symbol") or ""),
                    trade.get("side"),
                    trade.get("exit_time"),
                    _safe_json(trade),
                    1 if trade.get("provisional") else 0,
                    now,
                ),
            )
        return cursor.rowcount > 0

    def load_trade_results(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM live_trade_results ORDER BY exit_time, updated_at"
            ).fetchall()
        results = []
        for row in rows:
            try:
                results.append(json.loads(row["payload_json"]))
            except (TypeError, ValueError):
                logger.exception("Corrupt live trade result row")
        return results

    def close(self) -> None:
        with self._lock:
            self._connection.close()

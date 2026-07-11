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
import time
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
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    EMERGENCY_CLOSE_FAILED = "EMERGENCY_CLOSE_FAILED"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"


class OrderIntent(str, Enum):
    ENTRY = "ENTRY"
    POSITION_ADD = "POSITION_ADD"
    CLOSE = "CLOSE"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    PROTECTION_SL = "PROTECTION_SL"
    PROTECTION_TP = "PROTECTION_TP"
    GRID_ENTRY = "GRID_ENTRY"
    GRID_EXIT = "GRID_EXIT"


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
        OrderState.PARTIALLY_CLOSED.value,
        OrderState.EMERGENCY_CLOSE_FAILED.value,
    }
)
ENTRY_BLOCKING_STATES = frozenset(
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
        OrderState.PARTIALLY_CLOSED.value,
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


def _normalize_id_component(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def build_client_order_id(
    strategy: str,
    symbol: str,
    side: str,
    signal_timestamp: Any,
    stage: str = "entry",
    *,
    leg: str | None = None,
    revision: str | int | None = None,
    max_length: int = 36,
) -> str:
    """Return a Binance-safe deterministic client order ID."""

    strategy_full = _normalize_id_component(strategy) or "bot"
    symbol_full = _normalize_id_component(symbol).replace("usdtusdt", "usdt") or "coin"
    side_full = _normalize_id_component(side) or "unknown"
    stage_full = _normalize_id_component(stage) or "entry"
    leg_full = _normalize_id_component(leg)
    revision_full = _normalize_id_component(revision)
    timestamp_key = _normalize_signal_timestamp(signal_timestamp)
    digest_input = "|".join(
        (
            strategy_full,
            symbol_full,
            side_full,
            timestamp_key,
            stage_full,
            leg_full,
            revision_full,
        )
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:12]
    visible_side = "l" if side_full in {"long", "buy", "l"} else "s"
    visible_stage = stage_full[:5]
    visible_leg = leg_full[:3]
    prefix_parts = [strategy_full[:5], symbol_full[:10], visible_side, visible_stage]
    if visible_leg:
        prefix_parts.append(visible_leg)
    prefix = "-".join(prefix_parts)
    result = f"{prefix}-{digest}"
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
    order_intent: str = OrderIntent.ENTRY.value
    order_purpose: str = "entry"
    position_qty_before: float = 0.0
    position_signature: str | None = None
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
        self.order_intent = str(getattr(self.order_intent, "value", self.order_intent))
        self.order_purpose = str(self.order_purpose or "entry")
        self.position_qty_before = abs(float(self.position_qty_before or 0.0))
        self.requested_qty = float(self.requested_qty or 0.0)
        self.filled_qty = float(self.filled_qty or 0.0)
        self.average_fill_price = float(self.average_fill_price or 0.0)
        self.retry_count = int(self.retry_count or 0)
        self.updated_at = self.updated_at or utc_now_iso()
        return self


@dataclass(frozen=True)
class TradeUpsertResult:
    inserted: bool
    updated: bool
    finalized: bool
    trade_id: str


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
                    order_intent TEXT NOT NULL DEFAULT 'ENTRY',
                    order_purpose TEXT NOT NULL DEFAULT 'entry',
                    position_qty_before REAL NOT NULL DEFAULT 0,
                    position_signature TEXT,
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
                    finalized_at TEXT,
                    accounting_revision INTEGER NOT NULL DEFAULT 0,
                    last_accounting_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS global_entry_lease (
                    lease_id INTEGER PRIMARY KEY CHECK (lease_id = 1),
                    owner_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    client_order_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_event_high_water (
                    event_scope TEXT PRIMARY KEY,
                    client_order_id TEXT,
                    exchange_order_id TEXT,
                    last_event_time INTEGER NOT NULL DEFAULT 0,
                    last_transaction_time INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT,
                    last_cumulative_filled_qty REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._migrate_schema()

    def _table_columns(self, table: str) -> set[str]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError(f"invalid SQLite table name: {table}")
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
        return {str(row["name"]) for row in rows}

    def _migrate_schema(self) -> None:
        migrations = {
            "crypto_orders": {
                "order_intent": "TEXT NOT NULL DEFAULT 'ENTRY'",
                "order_purpose": "TEXT NOT NULL DEFAULT 'entry'",
                "position_qty_before": "REAL NOT NULL DEFAULT 0",
                "position_signature": "TEXT",
            },
            "live_trade_results": {
                "finalized_at": "TEXT",
                "accounting_revision": "INTEGER NOT NULL DEFAULT 0",
                "last_accounting_error": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing = self._table_columns(table)
            for column, definition in columns.items():
                if column in existing:
                    continue
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"  # nosec B608
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
            order_intent=row["order_intent"],
            order_purpose=row["order_purpose"],
            position_qty_before=row["position_qty_before"],
            position_signature=row["position_signature"],
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
                    signal_timestamp, requested_qty, order_intent, order_purpose,
                    position_qty_before, position_signature, filled_qty, average_fill_price,
                    order_state, stop_order_id, take_profit_order_ids_json, created_at,
                    updated_at, last_error, retry_count, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    exchange_order_id=excluded.exchange_order_id,
                    symbol=excluded.symbol,
                    side=excluded.side,
                    strategy=excluded.strategy,
                    signal_timestamp=excluded.signal_timestamp,
                    requested_qty=excluded.requested_qty,
                    order_intent=excluded.order_intent,
                    order_purpose=excluded.order_purpose,
                    position_qty_before=excluded.position_qty_before,
                    position_signature=excluded.position_signature,
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
                    record.order_intent,
                    record.order_purpose,
                    record.position_qty_before,
                    record.position_signature,
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
                    order_intent=?, order_purpose=?, position_qty_before=?, position_signature=?,
                    stop_order_id=?, take_profit_order_ids_json=?, updated_at=?, last_error=?,
                    retry_count=?, metadata_json=? WHERE client_order_id=?
                """,
                (
                    record.exchange_order_id,
                    record.filled_qty,
                    record.average_fill_price,
                    record.order_state,
                    record.order_intent,
                    record.order_purpose,
                    record.position_qty_before,
                    record.position_signature,
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

    def entry_block_reason(
        self,
        symbol: str | None = None,
        *,
        for_position_add: bool = False,
    ) -> str | None:
        records = self.list_by_states(ENTRY_BLOCKING_STATES)
        if for_position_add and symbol:
            normalized = _normalize_order_symbol(symbol)
            records = [
                record
                for record in records
                if not (
                    _normalize_order_symbol(record.symbol) == normalized
                    and record.order_state == OrderState.PROTECTED.value
                    and record.order_intent == OrderIntent.ENTRY.value
                )
            ]
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

    def try_acquire_entry_lease(
        self,
        owner_id: str,
        ttl_seconds: float,
        symbol: str,
        client_order_id: str,
        *,
        reconciliation_confirmed: bool = False,
        allow_same_symbol: bool = False,
    ) -> bool:
        now = time.time()
        expires_at = now + max(1.0, float(ttl_seconds or 0.0))
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT * FROM global_entry_lease WHERE lease_id=1"
                ).fetchone()
                if row is not None:
                    if str(row["client_order_id"]) == str(client_order_id):
                        self._connection.commit()
                        return True
                    if (
                        allow_same_symbol
                        and _normalize_order_symbol(row["symbol"])
                        == _normalize_order_symbol(symbol)
                    ):
                        self._connection.commit()
                        return True
                    expired = float(row["expires_at"] or 0.0) <= now
                    if not (expired and reconciliation_confirmed):
                        self._connection.rollback()
                        return False
                    self._connection.execute("DELETE FROM global_entry_lease WHERE lease_id=1")
                self._connection.execute(
                    """
                    INSERT INTO global_entry_lease(
                        lease_id, owner_id, acquired_at, expires_at, symbol, client_order_id
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (owner_id, now, expires_at, symbol, client_order_id),
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def get_entry_lease(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM global_entry_lease WHERE lease_id=1"
            ).fetchone()
        return dict(row) if row is not None else None

    def release_entry_lease(self, client_order_id: str, final_state: str | OrderState) -> bool:
        state = str(getattr(final_state, "value", final_state))
        if state not in {
            OrderState.FAILED.value,
            OrderState.CANCELED.value,
            OrderState.CLOSED.value,
        }:
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM global_entry_lease WHERE lease_id=1 AND client_order_id=?",
                (client_order_id,),
            )
        return cursor.rowcount > 0

    def release_entry_lease_for_symbol(
        self,
        symbol: str,
        *,
        reconciliation_confirmed: bool,
    ) -> bool:
        if not reconciliation_confirmed:
            return False
        lease = self.get_entry_lease()
        if not lease or _normalize_order_symbol(lease.get("symbol")) != _normalize_order_symbol(symbol):
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM global_entry_lease WHERE lease_id=1"
            )
        return cursor.rowcount > 0

    def get_order_event_high_water(self, event_scope: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM order_event_high_water WHERE event_scope=?",
                (event_scope,),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_order_event_high_water(
        self,
        event_scope: str,
        *,
        client_order_id: str | None,
        exchange_order_id: str | None,
        event_time: int,
        transaction_time: int,
        status: str | None,
        cumulative_filled_qty: float,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO order_event_high_water(
                    event_scope, client_order_id, exchange_order_id,
                    last_event_time, last_transaction_time, last_status,
                    last_cumulative_filled_qty, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_scope) DO UPDATE SET
                    client_order_id=excluded.client_order_id,
                    exchange_order_id=excluded.exchange_order_id,
                    last_event_time=excluded.last_event_time,
                    last_transaction_time=excluded.last_transaction_time,
                    last_status=excluded.last_status,
                    last_cumulative_filled_qty=excluded.last_cumulative_filled_qty,
                    updated_at=excluded.updated_at
                """,
                (
                    event_scope,
                    client_order_id,
                    exchange_order_id,
                    int(event_time or 0),
                    int(transaction_time or 0),
                    status,
                    float(cumulative_filled_qty or 0.0),
                    utc_now_iso(),
                ),
            )

    def record_trade_result(self, trade: dict[str, Any]) -> bool:
        return self.upsert_trade_result(trade).inserted

    def upsert_trade_result(self, trade: dict[str, Any]) -> TradeUpsertResult:
        trade_id = str(trade.get("trade_id") or "").strip()
        if not trade_id:
            raise ValueError("trade_id is required")
        now = utc_now_iso()
        incoming = dict(trade)
        incoming_provisional = bool(incoming.get("provisional"))
        incoming_revision = int(incoming.get("accounting_revision") or 0)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM live_trade_results WHERE trade_id=?",
                (trade_id,),
            ).fetchone()
            if row is None:
                finalized_at = None if incoming_provisional else now
                self._connection.execute(
                    """
                    INSERT INTO live_trade_results (
                        trade_id, client_order_id, strategy, engine, symbol, side,
                        exit_time, payload_json, provisional, finalized_at,
                        accounting_revision, last_accounting_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        incoming.get("client_order_id"),
                        incoming.get("strategy"),
                        incoming.get("engine"),
                        str(incoming.get("symbol") or ""),
                        incoming.get("side"),
                        incoming.get("exit_time"),
                        _safe_json(incoming),
                        1 if incoming_provisional else 0,
                        finalized_at,
                        incoming_revision,
                        incoming.get("last_accounting_error"),
                        now,
                    ),
                )
                return TradeUpsertResult(True, False, not incoming_provisional, trade_id)

            existing = json.loads(row["payload_json"] or "{}")
            existing_provisional = bool(row["provisional"])
            existing_revision = int(row["accounting_revision"] or 0)
            if not existing_provisional and incoming_provisional:
                return TradeUpsertResult(False, False, True, trade_id)
            if (
                not existing_provisional
                and not incoming_provisional
                and incoming_revision <= existing_revision
            ):
                return TradeUpsertResult(False, False, True, trade_id)

            merged = dict(existing)
            merged.update({key: value for key, value in incoming.items() if value is not None})
            if not incoming_provisional and "last_accounting_error" in incoming:
                merged["last_accounting_error"] = incoming.get("last_accounting_error")
            merged_provisional = existing_provisional and incoming_provisional
            revision = max(existing_revision, incoming_revision)
            changed = merged != existing or merged_provisional != existing_provisional
            if not changed and incoming_revision <= existing_revision:
                return TradeUpsertResult(False, False, not existing_provisional, trade_id)
            finalized_at = row["finalized_at"] or (None if merged_provisional else now)
            self._connection.execute(
                """
                UPDATE live_trade_results SET
                    client_order_id=?, strategy=?, engine=?, symbol=?, side=?,
                    exit_time=?, payload_json=?, provisional=?, finalized_at=?,
                    accounting_revision=?, last_accounting_error=?, updated_at=?
                WHERE trade_id=?
                """,
                (
                    merged.get("client_order_id"),
                    merged.get("strategy"),
                    merged.get("engine"),
                    str(merged.get("symbol") or ""),
                    merged.get("side"),
                    merged.get("exit_time"),
                    _safe_json(merged),
                    1 if merged_provisional else 0,
                    finalized_at,
                    revision,
                    merged.get("last_accounting_error"),
                    now,
                    trade_id,
                ),
            )
        return TradeUpsertResult(False, True, not merged_provisional, trade_id)

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

    def get_trade_result(self, trade_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM live_trade_results WHERE trade_id=?",
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"] or "{}")

    def load_provisional_trade_results(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json FROM live_trade_results
                WHERE provisional=1 ORDER BY updated_at LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [json.loads(row["payload_json"] or "{}") for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

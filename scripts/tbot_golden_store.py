#!/usr/bin/env python3
"""Generate golden store data from the Python reference implementation."""
from __future__ import annotations

import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import btc_ema_pullback.ledger as ledger_mod  # noqa: E402
from btc_ema_pullback.ledger import (  # noqa: E402
    StrategyLedger,
    compute_stats,
    daily_entry_block_reason,
    trading_day_bounds,
)
import trading_safety.order_state as order_state_mod  # noqa: E402
from trading_safety.order_state import (  # noqa: E402
    SQLiteTradingStateStore,
    OrderRecord,
    build_client_order_id,
)

FAKE_NOW = [datetime(2026, 10, 30, 1, 2, 3, 123456, tzinfo=timezone.utc)]
FAKE_EPOCH = [FAKE_NOW[0].timestamp()]


def _set_now(dt: datetime) -> None:
    FAKE_NOW[0] = dt
    FAKE_EPOCH[0] = dt.timestamp()


def _fake_utc_now():
    return FAKE_NOW[0]


def _fake_utc_now_iso() -> str:
    return FAKE_NOW[0].isoformat()


ledger_mod.utc_now = _fake_utc_now
order_state_mod.utc_now_iso = _fake_utc_now_iso
order_state_mod.time = types.SimpleNamespace(time=lambda: FAKE_EPOCH[0])


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_json(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return {k: _json_default(v) if not isinstance(v, (int, float, str, bool, type(None))) else v for k, v in row.items()}
    return row


def _record_to_json(rec: OrderRecord | None):
    if rec is None:
        return None
    from dataclasses import asdict
    return asdict(rec)


def _dump_tables(db_path: Path) -> list:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out = []
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid")]
    except Exception:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for name in names:
        if name.startswith("sqlite_"):
            continue
        rows = conn.execute(f"SELECT rowid, * FROM {name} ORDER BY rowid").fetchall()
        for r in rows:
            d = dict(r)
            d.pop("rowid", None)
            out.append({"table": name, "row": {k: _row_to_json(v) if not isinstance(v, (int, float, str, bool, type(None))) else v for k, v in d.items()}})
    conn.close()
    return out


UNSET = object()


def main(out_path: str) -> None:
    ops: list[dict] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        ledger = StrategyLedger(tmp_path / "ledger.sqlite3", tmp_path / "events.log")
        store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")

        def do(name: str, fn):
            _set_now(datetime(2026, 10, 30, 1, 2, 3, 123456, tzinfo=timezone.utc))
            # Record the clock the op starts from; ops may advance it themselves.
            started = FAKE_NOW[0].isoformat()
            try:
                result = fn()
                ops.append({"op": name, "now": started, "result": result})
            except Exception as e:  # noqa: BLE001
                ops.append({"op": name, "now": started,
                            "error": type(e).__name__ + ": " + str(e)})

        do("ledger_set_state", lambda: (
            ledger.set_state("day_state", {
                "status": "READY",
                "equity_krw": Decimal("1234567.89"),
                "signals": [1, 2, {"nested": "값"}],
                "memo": "한글 테스트 😀",
                "score": 1.5,
            }),
            ledger.get_state("day_state"),
        )[1])

        do("ledger_get_state_missing", lambda: ledger.get_state("missing", default="fallback"))

        def _upsert1():
            ledger.upsert_trade({
                "trade_id": "T1",
                "signal_id": "S1",
                "mode": "paper",
                "network": "mainnet",
                "status": "OPEN",
                "entry_time": "2026-10-30T01:00:00+00:00",
                "side": "LONG",
                "quantity": Decimal("0.125"),
                "entry_price": 100_000_000,
                "stop_price": 99_000_000.0,
                "take_profit_price": 101_000_000,
                "r_multiple": 1.0,
                "strategy_version": None,
                "extra": {"tag": "첫 거래"},
            })
            ledger.upsert_trade({
                "trade_id": "T1",
                "status": "CLOSED",
                "exit_time": "2026-10-30T02:00:00+00:00",
                "exit_price": 101_000_000,
                "gross_pnl": 500.5,
                "commission": 0.5,
                "funding": 0.0,
                "net_pnl": 500.0,
                "pnl_pct": 0.5,
                "result": "WIN",
                "exit_reason": "TP",
            })
            return ledger.get_trade("T1")

        do("ledger_upsert_trade", _upsert1)

        do("ledger_trades_mode", lambda: ledger.trades(mode="paper"))

        do("ledger_stats", lambda: compute_stats(ledger.trades()))

        def _events():
            ledger.event("started", bot="tbot", version="1")
            ledger.event("check", note="한글", value=1.5)
            ledger.event("done", ok=True)
            return ledger.recent_events(limit=2)

        do("ledger_events", _events)

        cfg = {
            "max_trades_per_day": 5,
            "max_consecutive_losses": 2,
            "max_daily_loss_pct": 0.03,
        }

        def _block1():
            trades_today = [
                {"status": "CLOSED", "net_pnl": "-100.0"},
                {"status": "CLOSED", "net_pnl": "-50.0"},
            ]
            return daily_entry_block_reason(
                trades_today, cfg, day_start_equity=1000.0, current_equity=990.0
            )

        do("ledger_daily_block_consecutive", _block1)

        def _block2():
            trades_today = [{"status": "CLOSED", "net_pnl": 100.0}]
            return daily_entry_block_reason(
                trades_today, cfg, day_start_equity=1000.0, current_equity=900.0
            )

        do("ledger_daily_block_loss_pct", _block2)

        def _block3():
            trades_today = [
                {"status": "CLOSED", "net_pnl": "10"},
                {"status": "CLOSED", "net_pnl": "20"},
                {"status": "CLOSED", "net_pnl": "30"},
                {"status": "CLOSED", "net_pnl": "40"},
                {"status": "CLOSED", "net_pnl": "50"},
            ]
            return daily_entry_block_reason(
                trades_today, cfg, day_start_equity=1000.0, current_equity=1000.0
            )

        do("ledger_daily_block_count", _block3)

        def _bounds_kst_before():
            now = datetime(2026, 10, 30, 14, 59, 59, tzinfo=timezone.utc)
            s, e, k = trading_day_bounds(now, "Asia/Seoul")
            return [s.isoformat(), e.isoformat(), k]

        do("ledger_bounds_kst_before", _bounds_kst_before)

        def _bounds_kst_after():
            now = datetime(2026, 10, 30, 15, 0, 1, tzinfo=timezone.utc)
            s, e, k = trading_day_bounds(now, "Asia/Seoul")
            return [s.isoformat(), e.isoformat(), k]

        do("ledger_bounds_kst_after", _bounds_kst_after)

        def _build_ids():
            return [
                build_client_order_id("btc_pullback", "BTC/USDT:USDT", "buy", 1700000000),
                build_client_order_id("btc_pullback", "BTC/USDT:USDT", "sell", "1700000000000"),
                build_client_order_id("btc_pullback", "BTC/USDT:USDT", "buy", "sig-123",
                                      stage="entry", leg="tp1", revision=2),
                build_client_order_id("x", "Y", "z", None, max_length=12),
            ]

        do("store_build_ids", _build_ids)

        def _upsert_new():
            rec = OrderRecord(
                created_at=_fake_utc_now_iso(),  # dataclass default bypasses the patched clock
                client_order_id="btcp-BTCUSDT-l-entr-AbCdEf123456",
                symbol="BTC/USDT:USDT",
                side="buy",
                strategy="btc_pullback",
                signal_timestamp="1700000000",
                requested_qty=0.01,
                order_intent="ENTRY",
                metadata={"note": "신규"},
            )
            return _record_to_json(store.upsert(rec))

        do("store_upsert_new", _upsert_new)

        def _transition_ok():
            rec = store.transition(
                "btcp-BTCUSDT-l-entr-AbCdEf123456",
                "ACKNOWLEDGED",
                exchange_order_id="EX-1",
                filled_qty=0.005,
            )
            return _record_to_json(rec)

        do("store_transition_ack", _transition_ok)

        def _transition_forbidden():
            return store.transition(
                "does-not-exist", "FILLED_UNPROTECTED", ordersize=1,
            )
            return None

        do("store_transition_unknown", _transition_forbidden)

        def _get():
            return _record_to_json(store.get("btcp-BTCUSDT-l-entr-AbCdEf123456"))

        do("store_get", _get)

        do("store_list_by_states", lambda: [
            _record_to_json(r) for r in
            store.list_by_states(["ACKNOWLEDGED", "PLANNED"])
        ])

        do("store_active", lambda: [
            _record_to_json(r) for r in
            store.active_for_symbol("BTCUSDT")
        ])

        def _block():
            return store.entry_block_reason("XRP/USDT:USDT")

        do("store_entry_block_reason", _block)

        def _lease():
            out = []
            out.append(store.try_acquire_entry_lease("owner-1", 60.0, "BTC/USDT", "cid-1"))
            out.append(store.try_acquire_entry_lease("owner-2", 60.0, "BTC/USDT", "cid-2"))
            _set_now(FAKE_NOW[0] + timedelta(seconds=120))
            out.append(store.try_acquire_entry_lease(
                "owner-3", 60.0, "BTC/USDT", "cid-3",
                reconciliation_confirmed=True,
            ))
            out.append(store.get_entry_lease())
            out.append(store.release_entry_lease("cid-3", "FAILED"))
            out.append(store.get_entry_lease())
            return out

        do("store_lease", _lease)

        def _runtime():
            store.set_runtime_state("k1", {"a": 1, "b": "값"})
            store.set_runtime_state("k2", "first")
            ins1 = store.create_runtime_state_if_absent("k2", "second")
            ins2 = store.create_runtime_state_if_absent("k3", ["x", "y"])
            got = store.get_runtime_state("k1")
            deleted = store.delete_runtime_state("k3")
            got3 = store.get_runtime_state("k3")
            return [ins1, ins2, got, deleted, got3]

        do("store_runtime", _runtime)

        def _daily_loss():
            store.set_runtime_state(
                "daily_loss_entry_lock",
                {"date": "2026-10-30", "reason": "LOSS_3"},
            )
            return store.daily_loss_entry_lock_reason()

        do("store_daily_loss_lock", _daily_loss)

        def _daily_loss_stale():
            store.set_runtime_state(
                "daily_loss_entry_lock",
                {"date": "2026-10-29", "reason": "OLD"},
            )
            return [store.daily_loss_entry_lock_reason(), store.get_runtime_state("daily_loss_entry_lock")]

        do("store_daily_loss_stale", _daily_loss_stale)

        # Dump both DBs
        tables = {}
        for dbname, dbpath in (("ledger", tmp_path / "ledger.sqlite3"),
                                ("state", tmp_path / "state.sqlite3")):
            import sqlite3
            conn = sqlite3.connect(str(dbpath))
            conn.row_factory = sqlite3.Row
            try:
                names = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid")]
            except Exception:
                names = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
            for name in names:
                if name.startswith("sqlite_"):
                    continue
                rows = conn.execute(f"SELECT rowid, * FROM {name} ORDER BY rowid").fetchall()
                table_rows = []
                for r in rows:
                    d = dict(r)
                    d.pop("rowid", None)  # order is kept by ORDER BY rowid
                    table_rows.append({k: (v if isinstance(v, (int, float, str, bool, type(None)))
                                          else _json_default(v)) for k, v in d.items()})
                tables[f"{dbname}:{name}"] = table_rows
            conn.close()

        events_path = tmp_path / "events.log"
        try:
            events = events_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            events = []

        def _finite(value):
            # JSON has no Infinity; the Rust port reports "inf" the same way.
            if isinstance(value, float) and value in (float("inf"), float("-inf")):
                return "inf" if value > 0 else "-inf"
            if isinstance(value, dict):
                return {k: _finite(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_finite(v) for v in value]
            return value

        out = _finite({"ops": ops, "tables": tables, "events": events})
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(out, handle, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    main(sys.argv[1])

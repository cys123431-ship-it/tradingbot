import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import emas
from trading_safety.manual_resume import CONFIRM_TOKEN, write_manual_resume_request
from trading_safety.order_gateway import IdempotentOrderGateway
from trading_safety.order_state import OrderRecord, OrderState, SQLiteTradingStateStore


class Exchange:
    id = "test"


def _controller(tmp_path, *, stream_ready, reconciliation, blocking_state=None):
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    store.set_runtime_state(
        "user_data_stream",
        {
            "connected": stream_ready,
            "reason": "connected_and_reconciled" if stream_ready else "disconnected",
        },
    )
    if blocking_state:
        store.upsert(
            OrderRecord(
                "blocking-1",
                "BTC/USDT:USDT",
                "LONG",
                "UTB",
                "1",
                1.0,
                order_state=blocking_state,
            )
        )
    exchange = Exchange()
    controller = SimpleNamespace(
        trading_state_store=store,
        exchange=exchange,
        active_engine=None,
        is_paused=True,
        _engine_performance_stats_restored=True,
    )
    gateway = IdempotentOrderGateway(exchange, store)
    controller.idempotent_order_gateway = gateway
    controller.crypto_execution_service = SimpleNamespace(exchange=exchange)
    engine = SimpleNamespace(
        ctrl=controller,
        exchange=exchange,
        trading_state_store=store,
        idempotent_order_gateway=gateway,
        crypto_entry_lock_reason="CRITICAL_PAUSE",
    )

    async def reconcile(**_kwargs):
        return reconciliation

    engine._reconcile_crypto_exchange_state = reconcile
    engine._set_crypto_entry_lock = lambda value: setattr(engine, "crypto_entry_lock_reason", value)
    controller.engines = {"signal": engine}

    async def notify(_message):
        return None

    controller.notify = notify
    return controller


def _request_and_pause(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = Path("runtime")
    runtime.mkdir()
    Path(emas.PAUSE_STATE_FILE).write_text(
        json.dumps({"symbol": "BTC/USDT:USDT", "reason": "test"}),
        encoding="utf-8",
    )
    write_manual_resume_request(
        "BTC/USDT:USDT",
        CONFIRM_TOKEN,
        requested_by="test",
    )


def test_manual_resume_rejects_when_stream_is_not_ready(tmp_path, monkeypatch):
    _request_and_pause(tmp_path, monkeypatch)
    reconciliation = SimpleNamespace(
        reconciled_at=None,
        issues=[],
        unresolved_records=[],
        positions=[],
        algo_orders_ok=True,
        safe_to_trade=True,
    )
    controller = _controller(
        tmp_path, stream_ready=False, reconciliation=reconciliation
    )
    result = asyncio.run(emas.MainController.process_manual_resume_request(controller))

    assert result["status"] == "RESUME_REJECTED_USER_STREAM_NOT_READY"
    assert Path(emas.PAUSE_STATE_FILE).exists()


def test_manual_resume_rejects_unresolved_order(tmp_path, monkeypatch):
    _request_and_pause(tmp_path, monkeypatch)
    reconciliation = SimpleNamespace(
        reconciled_at="2026-01-01T00:00:00Z",
        issues=[],
        unresolved_records=["submitted_unknown:blocking-1"],
        positions=[],
        algo_orders_ok=True,
        safe_to_trade=False,
    )
    controller = _controller(
        tmp_path,
        stream_ready=True,
        reconciliation=reconciliation,
        blocking_state=OrderState.SUBMITTED_UNKNOWN.value,
    )
    result = asyncio.run(emas.MainController.process_manual_resume_request(controller))

    assert result["status"] == "RESUME_REJECTED_UNRESOLVED_ORDER"
    assert Path(emas.PAUSE_STATE_FILE).exists()


def test_manual_resume_archives_pause_only_after_complete_reconciliation(tmp_path, monkeypatch):
    _request_and_pause(tmp_path, monkeypatch)
    reconciliation = SimpleNamespace(
        reconciled_at="2026-01-01T00:00:00Z",
        issues=[],
        unresolved_records=[],
        positions=[],
        algo_orders_ok=True,
        safe_to_trade=True,
    )
    controller = _controller(tmp_path, stream_ready=True, reconciliation=reconciliation)
    result = asyncio.run(emas.MainController.process_manual_resume_request(controller))

    assert result["status"] == "RESUME_APPROVED"
    assert result["approved"] is True
    assert not Path(emas.PAUSE_STATE_FILE).exists()
    assert list(Path("runtime/manual_resume_archive").glob("critical_pause_*.json"))


def test_standalone_resume_script_only_writes_request():
    source = Path(emas.__file__).parent.joinpath("scripts/manual_resume.py").read_text(
        encoding="utf-8"
    )
    assert "write_manual_resume_request" in source
    assert "archive_critical_pause" not in source
    assert "unlink(" not in source
    assert "shutil.move" not in source

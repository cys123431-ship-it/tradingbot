import asyncio
import json
from types import SimpleNamespace

import pytest

import emas
from trading_safety.entry_block import CriticalPauseBlockDecision, build_critical_pause_notice_key
from trading_safety.order_state import SQLiteTradingStateStore


def _owner(store, notifier):
    engine = object.__new__(emas.SignalEngine)
    engine.last_entry_reason = {}
    engine.trading_state_store = store
    engine.exchange = SimpleNamespace(id="test")
    engine._utbreakout_trace_event = lambda *args, **kwargs: None
    engine.ctrl = SimpleNamespace(notify=notifier, trading_state_store=store, is_paused=False)
    return engine


def test_atomic_notice_once_for_concurrent_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, "_ensure_trading_safety_runtime", lambda owner: None)
    store = SQLiteTradingStateStore(tmp_path / "state.db")
    messages = []
    async def notify(message): messages.append(message)
    owner = _owner(store, notify)
    decision = CriticalPauseBlockDecision(True, "R", "same-pause", "GLOBAL", "HMSTR/USDT:USDT")

    async def run():
        await asyncio.gather(*[
            emas._handle_critical_pause_entry_block(
                owner,
                symbol=symbol,
                raw_symbol=symbol,
                side="buy",
                decision=decision,
                qty=None,
                phase="ENTRY_PREFLIGHT",
            )
            for symbol in ("AVAXUSDT", "PARTIUSDT", "XLMUSDT", "TAOUSDT", "ONDOUSDT")
        ])
    asyncio.run(run())
    assert len(messages) == 1
    key = build_critical_pause_notice_key(decision, requested_symbol="AVAXUSDT")
    assert store.get_runtime_state(key)["status"] == "SENT"


def test_failed_notification_releases_exact_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, "_ensure_trading_safety_runtime", lambda owner: None)
    store = SQLiteTradingStateStore(tmp_path / "state.db")
    attempts = {"count": 0}
    async def notify(message):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("telegram down")
    owner = _owner(store, notify)
    decision = CriticalPauseBlockDecision(True, "R", "retry-pause", "GLOBAL", "*")
    kwargs = dict(owner=owner, symbol="BTCUSDT", raw_symbol="BTCUSDT", side="buy", decision=decision, qty=None, phase="P")
    asyncio.run(emas._handle_critical_pause_entry_block(**kwargs))
    key = build_critical_pause_notice_key(decision, requested_symbol="BTCUSDT")
    assert store.get_runtime_state(key) is None
    asyncio.run(emas._handle_critical_pause_entry_block(**kwargs))
    assert attempts["count"] == 2
    assert store.get_runtime_state(key)["status"] == "SENT"


def test_writer_preserves_legacy_incident_and_rejects_corruption(tmp_path, monkeypatch):
    pause_file = tmp_path / "pause.json"
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(pause_file))
    pause_file.write_text(json.dumps({
        "symbol": "BTC/USDT:USDT",
        "reason": "test",
        "timestamp": "2025-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    first = emas.write_critical_pause_state("BTC/USDT:USDT", "test", RuntimeError("x"))
    second = emas.write_critical_pause_state("BTC/USDT:USDT", "test", RuntimeError("x"))
    assert first["pause_id"] == second["pause_id"]
    assert first["created_at"] == "2025-01-01T00:00:00+00:00"
    assert second["occurrence_count"] == first["occurrence_count"] + 1

    pause_file.write_bytes(b"{broken")
    before = pause_file.read_bytes()
    with pytest.raises(RuntimeError, match="WRITE_BLOCKED_BY_UNREADABLE"):
        emas.write_critical_pause_state("BTC/USDT:USDT", "new", RuntimeError("x"))
    assert pause_file.read_bytes() == before
    assert emas.load_critical_pause_state()["scope"] == "GLOBAL"

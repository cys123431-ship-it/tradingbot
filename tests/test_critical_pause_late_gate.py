import asyncio
import json
from types import SimpleNamespace

import pytest

import emas
from trading_safety.entry_block import CriticalPauseBlockDecision, EntrySubmitOutcome
from trading_safety.order_state import OrderState, SQLiteTradingStateStore


def _engine(tmp_path, service):
    class Exchange:
        id = "test"

    class Controller:
        is_paused = False
        trading_state_store = SQLiteTradingStateStore(tmp_path / "state.db")
        crypto_execution_service = service

    engine = object.__new__(emas.SignalEngine)
    engine.ctrl = Controller()
    engine.exchange = Exchange()
    service.exchange = engine.exchange
    engine.trading_state_store = engine.ctrl.trading_state_store
    engine.crypto_entry_lock_reason = None
    return engine


def test_outcome_invariants():
    with pytest.raises(ValueError):
        EntrySubmitOutcome()
    decision = CriticalPauseBlockDecision(True, "R", "P", "GLOBAL", "*")
    with pytest.raises(ValueError):
        EntrySubmitOutcome(critical_pause_block=decision, entry_block_reason="x")
    with pytest.raises(ValueError):
        EntrySubmitOutcome(duplicate_protected=True)
    with pytest.raises(ValueError):
        EntrySubmitOutcome(submission_error="x")


def test_gateway_blocked_rechecks_latest_pause(tmp_path, monkeypatch):
    pause_file = tmp_path / "pause.json"
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(pause_file))

    class Service:
        exchange = None
        async def submit_entry(self, **kwargs):
            pause_file.write_text(json.dumps({
                "status": "CRITICAL_PAUSED",
                "scope": "GLOBAL",
                "reason_code": "RACE_PAUSE",
                "origin_symbol": "BTC/USDT:USDT",
                "pause_id": "race",
            }), encoding="utf-8")
            return SimpleNamespace(state="BLOCKED", accepted=False, recovered=False, error="blocked", client_order_id="cid")

    engine = _engine(tmp_path, Service())
    outcome = asyncio.run(emas._submit_idempotent_crypto_entry(engine, "ETH/USDT:USDT", "buy", 1.0, "UT"))
    assert outcome.critical_pause_block is not None
    assert outcome.submission is None


def test_gateway_result_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(tmp_path / "missing.json"))

    class Service:
        exchange = None
        def __init__(self, result): self.result = result
        async def submit_entry(self, **kwargs): return self.result

    acknowledged = SimpleNamespace(state=OrderState.ACKNOWLEDGED, accepted=True, recovered=False, error=None, client_order_id="a")
    engine = _engine(tmp_path, Service(acknowledged))
    result = asyncio.run(emas._submit_idempotent_crypto_entry(engine, "BTC/USDT:USDT", "buy", 1.0, "UT"))
    assert result.submission is acknowledged

    protected = SimpleNamespace(state=OrderState.PROTECTED, accepted=True, recovered=True, error=None, client_order_id="p")
    engine.ctrl.crypto_execution_service.result = protected
    result = asyncio.run(emas._submit_idempotent_crypto_entry(engine, "BTC/USDT:USDT", "buy", 1.0, "UT"))
    assert result.duplicate_protected
    assert result.submission is protected

    unknown = SimpleNamespace(state=OrderState.SUBMITTED_UNKNOWN, accepted=False, recovered=False, error="unknown", client_order_id="u")
    engine.ctrl.crypto_execution_service.result = unknown
    result = asyncio.run(emas._submit_idempotent_crypto_entry(engine, "BTC/USDT:USDT", "buy", 1.0, "UT"))
    assert result.submission is unknown
    assert result.submission_error == "unknown"

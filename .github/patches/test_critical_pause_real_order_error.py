import asyncio
from types import SimpleNamespace

import ccxt
import pytest

import emas
from trading_safety.order_state import SQLiteTradingStateStore


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


def test_direct_invalid_order_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(tmp_path / "none.json"))
    class Service:
        exchange = None
        async def submit_entry(self, **kwargs):
            raise ccxt.InvalidOrder("bad order")
    engine = _engine(tmp_path, Service())
    with pytest.raises(ccxt.InvalidOrder):
        asyncio.run(emas._submit_idempotent_crypto_entry(engine, "BTC/USDT:USDT", "buy", 1.0, "UT"))


def test_missing_result_is_internal_error(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(tmp_path / "none.json"))
    class Service:
        exchange = None
        async def submit_entry(self, **kwargs): return None
    engine = _engine(tmp_path, Service())
    with pytest.raises(RuntimeError, match="ENTRY_SUBMISSION_RESULT_MISSING"):
        asyncio.run(emas._submit_idempotent_crypto_entry(engine, "BTC/USDT:USDT", "buy", 1.0, "UT"))


def test_sparse_failed_result_uses_defensive_access(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(tmp_path / "none.json"))
    class Service:
        exchange = None
        async def submit_entry(self, **kwargs): return SimpleNamespace(state="FAILED")
    engine = _engine(tmp_path, Service())
    outcome = asyncio.run(emas._submit_idempotent_crypto_entry(engine, "BTC/USDT:USDT", "buy", 1.0, "UT"))
    assert outcome.submission_error == "FAILED"

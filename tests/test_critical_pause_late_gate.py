import asyncio
import json
from types import SimpleNamespace

import pytest

import emas
from trading_safety.entry_block import CriticalPauseBlockDecision, EntrySubmitOutcome
from trading_safety.order_gateway import IdempotentOrderGateway
from trading_safety.order_state import (
    OrderIntent,
    OrderRecord,
    OrderState,
    SQLiteTradingStateStore,
    build_client_order_id,
)


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


def test_closed_signal_replay_is_an_entry_block_not_an_order_failure(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(tmp_path / "missing.json"))
    signal_timestamp = 1_700_000_000_000
    strategy = "ema200_utbot_rsi_2h"
    symbol = "SOL/USDT:USDT"
    client_order_id = build_client_order_id(
        strategy,
        symbol,
        "long",
        signal_timestamp,
        "entry",
    )
    store = SQLiteTradingStateStore(tmp_path / "state.db")
    store.upsert(
        OrderRecord(
            client_order_id=client_order_id,
            symbol=symbol,
            side="LONG",
            strategy=strategy,
            signal_timestamp=str(signal_timestamp),
            requested_qty=2.61,
            order_intent=OrderIntent.ENTRY.value,
            order_purpose="entry",
            order_state=OrderState.CLOSED.value,
        )
    )
    service = IdempotentOrderGateway(SimpleNamespace(id="test"), store)
    engine = _engine(tmp_path, service)
    outcome = asyncio.run(
        emas._submit_idempotent_crypto_entry(
            engine,
            symbol,
            "long",
            2.61,
            strategy,
            {"signal_timestamp": signal_timestamp},
        )
    )

    assert outcome.entry_block_reason == "SIGNAL_ALREADY_HANDLED:CLOSED"
    assert outcome.submission is None
    assert outcome.submission_error is None
    assert outcome.client_order_id == client_order_id


def test_small_account_adaptive_entry_marks_daily_loss_exemption(tmp_path, monkeypatch):
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(tmp_path / "missing.json"))

    class Service:
        exchange = None
        kwargs = None

        async def submit_entry(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                state=OrderState.ACKNOWLEDGED,
                accepted=True,
                recovered=False,
                error=None,
                client_order_id="small-account",
            )

    service = Service()
    engine = _engine(tmp_path, service)
    payload = {
        "small_account_aggressive_active": True,
        "small_account_equity_usdt": 80.0,
        "small_account_equity_threshold_usdt": 1_000.0,
    }

    result = asyncio.run(
        emas._submit_idempotent_crypto_entry(
            engine,
            "BTC/USDT:USDT",
            "buy",
            1.0,
            "adaptive_breakout_trend_v1",
            payload,
        )
    )

    assert result.submission is not None
    assert service.kwargs["daily_loss_exempt"] is True

import asyncio
import json

import emas


def _build_engine(tmp_path):
    engine = object.__new__(emas.SignalEngine)
    engine.runtime_dir = str(tmp_path)
    engine.utbreakout_daily_sl_symbol_lockouts = {}
    engine.utbreakout_entry_trace = {}
    engine.utbreakout_last_ready_ts = {}
    engine.utbreakout_last_ready_side = {}
    engine.utbreakout_last_order_attempt_ts = {}
    engine.utbreakout_last_watchdog_report_ts = {}
    engine.utbreakout_trace_watchdog_enabled = True
    engine.last_entry_reason = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }
    engine._get_coin_selector_config = lambda: {"enabled": False}
    engine.trade_direction = "both"

    class Controller:
        is_paused = False
        messages = []

        async def notify(self, message):
            self.messages.append(message)

    engine.ctrl = Controller()
    return engine


def _eligible_kwargs(symbol, side):
    return {
        "symbol": symbol,
        "side": side,
        "candidate_side": side,
        "candidate_type": "fresh_signal",
        "side_condition_ok": True,
        "risk_ok": True,
        "planned_qty": 1.0,
        "risk_usdt": 1.0,
        "entry_plan_detail": "ok",
        "cooldown_reasons": [],
        "has_open_position": False,
        "has_other_position": False,
        "auto_entry_enabled": True,
        "daily_risk_ok": True,
        "plan_lookup_ready": True,
        "cfg": {"utbreakout_require_scanner_candidate_for_auto_entry": True},
        "scanner_source": "scanner_seen",
        "is_live_scanner_context": True,
        "is_current_scanner_candidate": True,
        "is_coinselector_top_candidate": True,
        "next_scan_symbol": symbol,
        "evaluated_symbol": symbol,
    }


def test_daily_sl_lockout_blocks_long_and_short_for_same_symbol_only(tmp_path):
    engine = _build_engine(tmp_path)

    engine._record_utbreakout_daily_sl_lockout(
        "DOGEUSDT",
        side="short",
        reason="STOP_LOSS_FILLED",
        detail="closed SL order",
    )

    locked, reason = engine._is_utbreakout_daily_sl_locked("DOGE/USDT:USDT")
    assert locked is True
    assert "STOP_LOSS_FILLED" in reason

    for side in ("long", "short"):
        eligibility = engine._build_utbreakout_execution_eligibility(
            **_eligible_kwargs("DOGE/USDT:USDT", side)
        )
        assert eligibility["can_attempt"] is False
        assert any("daily SL lockout" in blocker for blocker in eligibility["blockers"])

    other = engine._build_utbreakout_execution_eligibility(
        **_eligible_kwargs("ETH/USDT:USDT", "short")
    )
    assert other["can_attempt"] is True


def test_daily_sl_lockout_status_blocker_keeps_reason_for_display(tmp_path):
    engine = _build_engine(tmp_path)
    engine._record_utbreakout_daily_sl_lockout(
        "DOGEUSDT",
        side="long",
        reason="STOP_LOSS_FILLED",
    )
    eligibility = engine._build_utbreakout_execution_eligibility(
        **_eligible_kwargs("DOGE/USDT:USDT", "short")
    )

    display = engine._format_utbreakout_execution_blockers_for_display(
        "short",
        [],
        eligibility,
    )

    assert any("daily SL lockout" in item for item in display)
    assert any("당일 SL lockout" in item for item in display)
    assert any("STOP_LOSS_FILLED" in item for item in display)


def test_daily_sl_lockout_blocks_direct_entry_before_order_attempt(tmp_path):
    engine = _build_engine(tmp_path)
    engine._record_utbreakout_daily_sl_lockout(
        "SOLUSDT",
        side="short",
        reason="STOP_LOSS_FILLED",
    )

    asyncio.run(engine.entry("SOL/USDT:USDT", "short", 98.5))

    events = engine._utbreakout_recent_trace_events("SOLUSDT", limit=20)
    assert any(
        event["stage"] == "ENTRY_BLOCKED"
        and event["status"] == "DAILY_SL_LOCKOUT"
        for event in events
    )
    assert not any(event["stage"] == "ORDER_ATTEMPT" for event in events)
    assert "STOP_LOSS_FILLED" in engine.last_entry_reason["SOL/USDT:USDT"]


def test_daily_sl_lockout_persists_and_expires_by_day(tmp_path):
    engine = _build_engine(tmp_path)
    engine._utbreakout_today_key = lambda: "2026-06-29"
    engine._record_utbreakout_daily_sl_lockout(
        "BTC/USDT",
        side="long",
        reason="STOP_LOSS_FILLED",
    )

    path = tmp_path / "utbreakout_daily_sl_lockouts.json"
    assert path.exists()

    reloaded = _build_engine(tmp_path)
    reloaded._utbreakout_today_key = lambda: "2026-06-29"
    reloaded._load_utbreakout_daily_sl_lockouts()
    locked, _ = reloaded._is_utbreakout_daily_sl_locked("BTCUSDT")
    assert locked is True

    reloaded._utbreakout_today_key = lambda: "2026-06-30"
    locked, _ = reloaded._is_utbreakout_daily_sl_locked("BTCUSDT")
    assert locked is False
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_sl_fill_lockout_uses_exchange_order_status_or_stop_price(tmp_path):
    engine = _build_engine(tmp_path)

    class Exchange:
        def __init__(self, status):
            self.status = status

        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": self.status}

    state = {
        "side": "long",
        "sl_order_id": "sl-1",
        "last_stop_price": 90.0,
    }

    engine.exchange = Exchange("open")
    asyncio.run(
        engine._check_and_record_sl_lockout_async(
            "SOL/USDT:USDT",
            state,
            exit_price=110.0,
        )
    )
    locked, _ = engine._is_utbreakout_daily_sl_locked("SOLUSDT")
    assert locked is False

    engine.exchange = Exchange("closed")
    asyncio.run(
        engine._check_and_record_sl_lockout_async(
            "SOL/USDT:USDT",
            state,
            exit_price=110.0,
        )
    )
    locked, reason = engine._is_utbreakout_daily_sl_locked("SOLUSDT")
    assert locked is True
    assert "STOP_LOSS_FILLED" in reason


def test_stop_price_fill_fallback_records_lockout_without_order_id(tmp_path):
    engine = _build_engine(tmp_path)
    state = {
        "side": "short",
        "last_stop_price": 105.0,
    }

    asyncio.run(
        engine._check_and_record_sl_lockout_async(
            "XRP/USDT:USDT",
            state,
            exit_price=105.1,
        )
    )

    locked, reason = engine._is_utbreakout_daily_sl_locked("XRPUSDT")
    assert locked is True
    assert "STOP_LOSS_FILLED" in reason

import asyncio
import json
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

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


def _insert_automatic_trade(db, *, symbol, entry_time, exit_time=None):
    with db.lock:
        db.conn.execute(
            """INSERT INTO trades (symbol, side, entry_price, quantity,
            entry_time, exit_time, strategy) VALUES (?, 'long', 100, 1, ?, ?, ?)""",
            (symbol, entry_time.isoformat(),
             exit_time.isoformat() if exit_time else None,
             'ema200_utbot_rsi_2h'),
        )
        db.conn.commit()


def test_automatic_symbol_reentry_waits_one_hour_from_confirmed_close(tmp_path):
    db = emas.DBManager(str(tmp_path / 'trades.db'))
    engine = _build_engine(tmp_path)
    engine.db = db
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    _insert_automatic_trade(
        db, symbol='BTC/USDT:USDT', entry_time=closed_at - timedelta(hours=2),
        exit_time=closed_at,
    )

    locked, reason = engine._is_automatic_daily_symbol_entry_locked('BTCUSDT')
    assert locked and '1시간' in reason
    assert engine._build_utbreakout_execution_eligibility(
        **_eligible_kwargs('BTC/USDT:USDT', 'short')
    )['can_attempt'] is False
    assert engine._is_automatic_daily_symbol_entry_locked('ETHUSDT')[0] is False

    # Restart with a separate engine and SQLite connection.  The completed
    # close timestamp, not the entry date or in-memory lock, is authoritative.
    db.conn.close()
    restarted = _build_engine(tmp_path)
    restarted.db = emas.DBManager(str(tmp_path / 'trades.db'))
    assert restarted._is_automatic_daily_symbol_entry_locked('BTCUSDT')[0]
    boundary = closed_at + timedelta(hours=1)
    assert restarted._is_automatic_daily_symbol_entry_locked(
        'BTCUSDT', now=boundary - timedelta(microseconds=1)
    )[0]
    assert restarted._is_automatic_daily_symbol_entry_locked(
        'BTCUSDT', now=boundary
    )[0] is False


def test_automatic_symbol_reentry_does_not_expire_before_confirmed_close(tmp_path):
    engine = _build_engine(tmp_path)
    engine.db = emas.DBManager(str(tmp_path / 'trades.db'))
    now = datetime.now(timezone.utc)
    _insert_automatic_trade(
        engine.db, symbol='SOL/USDT:USDT', entry_time=now - timedelta(hours=4),
    )
    locked, reason = engine._is_automatic_daily_symbol_entry_locked(
        'SOLUSDT', now=now,
    )
    assert locked and '청산' in reason


def test_automatic_symbol_reentry_waits_across_korea_midnight(tmp_path):
    engine = _build_engine(tmp_path)
    engine.db = emas.DBManager(str(tmp_path / 'trades.db'))
    kst = ZoneInfo('Asia/Seoul')
    closed_at = datetime(2026, 9, 24, 23, 45, tzinfo=kst)
    _insert_automatic_trade(
        engine.db, symbol='DOGE/USDT:USDT',
        entry_time=closed_at - timedelta(hours=2), exit_time=closed_at,
    )
    assert engine._is_automatic_daily_symbol_entry_locked(
        'DOGEUSDT', now=closed_at + timedelta(minutes=30),
    )[0]
    assert engine._is_automatic_daily_symbol_entry_locked(
        'DOGEUSDT', now=closed_at + timedelta(hours=1),
    )[0] is False


def test_ema_direct_entry_is_blocked_during_symbol_reentry_wait(tmp_path):
    engine = _build_engine(tmp_path)
    engine.get_runtime_strategy_params = lambda: {
        'active_strategy': emas.EMA200_UTBOT_RSI_STRATEGY,
    }
    engine.db = emas.DBManager(str(tmp_path / 'trades.db'))
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    _insert_automatic_trade(
        engine.db, symbol='BTC/USDT:USDT',
        entry_time=closed_at - timedelta(hours=2), exit_time=closed_at,
    )

    asyncio.run(engine.entry('BTC/USDT:USDT', 'long', 100.0))

    assert '1시간 재진입 대기' in engine.last_entry_reason['BTC/USDT:USDT']


def test_automatic_symbol_lock_stays_closed_while_fill_is_missing_from_db(tmp_path):
    engine = _build_engine(tmp_path)
    engine.db = emas.DBManager(str(tmp_path / 'trades.db'))
    old_close = datetime.now(timezone.utc) - timedelta(hours=3)
    _insert_automatic_trade(
        engine.db, symbol='BTC/USDT:USDT',
        entry_time=old_close - timedelta(hours=1), exit_time=old_close,
    )
    engine._record_automatic_daily_symbol_entry_lock(
        'BTCUSDT', side='long', strategy='ema200_utbot_rsi_2h'
    )
    assert engine._is_automatic_daily_symbol_entry_locked('BTCUSDT')[0]

    restarted = _build_engine(tmp_path)
    restarted.db = emas.DBManager(str(tmp_path / 'trades.db'))
    restarted._utbreakout_today_key = lambda: '2026-09-27'
    restarted._load_utbreakout_daily_sl_lockouts()
    assert restarted._is_automatic_daily_symbol_entry_locked('BTCUSDT')[0]


def test_automatic_symbol_entry_history_lookup_error_blocks_entry(tmp_path):
    engine = _build_engine(tmp_path)

    class FailingDB:
        def get_latest_automatic_symbol_trade(self, _symbol):
            raise RuntimeError('test DB failure')

    engine.db = FailingDB()
    locked, reason = engine._is_automatic_daily_symbol_entry_locked('BTCUSDT')
    assert locked and '이력을 확인할 수 없어' in reason


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
        assert any(
            "daily symbol entry lockout" in blocker
            for blocker in eligibility["blockers"]
        )

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

    assert any("daily symbol entry lockout" in item for item in display)
    assert any("당일 종목 재진입 금지" in item for item in display)
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
        and event["status"] == "DAILY_SYMBOL_ENTRY_LOCKOUT"
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


def test_confirmed_automatic_entry_without_db_stays_locked_across_kst_midnight(tmp_path):
    engine = _build_engine(tmp_path)
    engine._utbreakout_today_key = lambda: "2026-09-01"

    engine._record_automatic_daily_symbol_entry_lock(
        "HEMI/USDT:USDT",
        side="long",
        strategy="adaptive_breakout_trend_v1",
        detail="confirmed_qty=100",
    )

    for side in ("long", "short"):
        locked, reason = engine._is_automatic_daily_symbol_entry_locked(
            "HEMIUSDT"
        )
        assert locked is True
        assert "청산을 확인할 수 없어" in reason
        eligibility = engine._build_utbreakout_execution_eligibility(
            **_eligible_kwargs("HEMI/USDT:USDT", side)
        )
        assert eligibility["can_attempt"] is False

    engine._utbreakout_today_key = lambda: "2026-09-02"
    locked, _ = engine._is_automatic_daily_symbol_entry_locked("HEMIUSDT")
    assert locked is True

    restarted = _build_engine(tmp_path)
    restarted._utbreakout_today_key = lambda: "2026-09-02"
    restarted._load_utbreakout_daily_sl_lockouts()
    assert restarted._is_automatic_daily_symbol_entry_locked("HEMIUSDT")[0]


def test_automatic_symbol_without_confirmed_close_blocks_from_legacy_db(tmp_path):
    engine = _build_engine(tmp_path)

    class DB:
        def __init__(self):
            self.calls = []

        def get_daily_automatic_symbol_entry(self, symbol):
            self.calls.append(symbol)
            return {
                "symbol": "SOL/USDT:USDT",
                "side": "long",
                "entry_time": "2026-09-01T00:00:00+00:00",
                "strategy": "adaptive_breakout_trend_v1",
            }

    engine.db = DB()
    locked, reason = engine._is_automatic_daily_symbol_entry_locked("SOLUSDT")

    assert locked is True
    assert "청산 확인 전" in reason
    assert engine.db.calls == ["SOL/USDT:USDT"]
    assert engine.utbreakout_daily_sl_symbol_lockouts == {}


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


def test_profitable_trailing_stop_does_not_create_daily_loss_lockout(tmp_path):
    engine = _build_engine(tmp_path)

    class Exchange:
        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": "closed"}

    engine.exchange = Exchange()
    state = {
        "side": "long",
        "entry_price": 100.0,
        "sl_order_id": "profit-lock-stop",
        "last_stop_price": 105.0,
    }

    asyncio.run(
        engine._check_and_record_sl_lockout_async(
            "HYPE/USDT:USDT",
            state,
            exit_price=105.0,
        )
    )

    locked, _ = engine._is_utbreakout_daily_sl_locked("HYPEUSDT")
    assert locked is False
    events = engine._utbreakout_recent_trace_events("HYPEUSDT", limit=20)
    assert any(
        event["stage"] == "DAILY_SL_LOCKOUT"
        and event["status"] == "SKIPPED_PROFITABLE_STOP"
        for event in events
    )

"""EMA200 session status panel and the morning entry-state reset."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import emas
from bot_runtime import ema200_session
from bot_runtime.database import DBManager
from bot_runtime.ema200_session import (
    EMA200_MORNING_RESET_STATE_KEY,
    ema200_entry_count_since,
    ema200_morning_reset_availability,
    perform_ema200_morning_entry_reset,
)
from bot_runtime.ema200_utbot_rsi import (
    EMA200_DAILY_LOSS_RESET_STATE_KEY,
    EMA200_KST,
    EMA200_UTBOT_RSI_STRATEGY,
    apply_ema200_daily_loss_reset,
    get_ema200_consecutive_losses,
)
from trading_safety.order_state import (
    DAILY_LOSS_ENTRY_LOCK_KEY,
    SQLiteTradingStateStore,
)
from bot_runtime.ema200_utbot_rsi import EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY


def _kst(hour, minute=0):
    today = datetime.now(timezone.utc).astimezone(EMA200_KST)
    return today.replace(hour=hour, minute=minute, second=0, microsecond=0)


@pytest.fixture
def ledger(tmp_path):
    db = DBManager(str(tmp_path / "trades.db"))
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    yield db, store
    db.conn.close()
    store.close()


@pytest.fixture
def any_hour(monkeypatch):
    # DB day windows follow the real clock, so reset "now" must too.
    monkeypatch.setattr(ema200_session, "EMA200_MORNING_RESET_CUTOFF_HOUR_KST", 24)


def _close(db, symbol, pnl, strategy=EMA200_UTBOT_RSI_STRATEGY):
    db.log_trade_entry(symbol, "short", 100.0, 1.0, strategy=strategy)
    assert db.log_trade_close(symbol, pnl, pnl, 100.0 - pnl, "test")


@pytest.mark.parametrize("hour,minute,allowed", [
    (0, 0, True), (9, 30, True), (11, 59, True), (12, 0, False), (23, 0, False),
])
def test_reset_is_available_only_in_the_korean_morning(ledger, hour, minute, allowed):
    _, store = ledger
    ok, reason = ema200_morning_reset_availability(store, _kst(hour, minute))
    assert ok is allowed
    if not allowed:
        assert "오전" in reason


def test_reset_returns_to_first_entry_state_once_per_day(ledger, any_hour):
    db, store = ledger
    for symbol in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"):
        _close(db, symbol, -4.0)
    store.set_runtime_state(DAILY_LOSS_ENTRY_LOCK_KEY, {"reason": "test"})
    assert get_ema200_consecutive_losses(db)[0] == 3
    assert db.get_daily_automatic_entry_count() == 3

    payload = perform_ema200_morning_entry_reset(db, store)

    assert payload["automatic_entries_before"] == 3
    assert payload["consecutive_losses_before"] == 3
    assert payload["daily_realized_pnl_before"] == pytest.approx(-12.0)
    streak, reset_active = get_ema200_consecutive_losses(
        db, store.get_runtime_state(EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY)
    )
    assert (streak, reset_active) == (0, True)
    effective_daily, _, active = apply_ema200_daily_loss_reset(
        db.get_daily_stats()[1],
        store.get_runtime_state(EMA200_DAILY_LOSS_RESET_STATE_KEY),
    )
    assert active and effective_daily == pytest.approx(0.0)
    assert store.get_runtime_state(DAILY_LOSS_ENTRY_LOCK_KEY) is None
    since = ema200_entry_count_since(store)
    assert db.get_daily_automatic_entry_count(since=since) == 0
    # History is preserved; only the effective baselines moved.
    assert db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3

    with pytest.raises(ValueError, match="이미 사용"):
        perform_ema200_morning_entry_reset(db, store)

    # New activity after the reset counts again from zero.
    _close(db, "XRP/USDT:USDT", -1.0)
    assert db.get_daily_automatic_entry_count(since=since) == 1
    assert get_ema200_consecutive_losses(
        db, store.get_runtime_state(EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY)
    )[0] == 1


def test_reset_is_refused_in_the_afternoon(ledger):
    db, store = ledger
    with pytest.raises(ValueError, match="오전"):
        perform_ema200_morning_entry_reset(db, store, now=_kst(13))
    assert store.get_runtime_state(EMA200_MORNING_RESET_STATE_KEY) is None


def test_entry_count_cutoff_ignores_a_previous_day_reset(ledger):
    _, store = ledger
    store.set_runtime_state(EMA200_MORNING_RESET_STATE_KEY, {
        "kst_date": (_kst(9) - timedelta(days=1)).date().isoformat(),
        "reset_at": (_kst(9) - timedelta(days=1)).isoformat(),
    })
    assert ema200_entry_count_since(store) is None


def test_signal_engine_daily_count_honors_todays_reset(ledger, any_hour):
    db, store = ledger
    for symbol in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
        _close(db, symbol, -1.0)
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.db = db
    engine.trading_state_store = store
    engine.ctrl = None
    assert int(engine.get_automatic_daily_entry_count()) == 2

    perform_ema200_morning_entry_reset(db, store)

    assert int(engine.get_automatic_daily_entry_count()) == 0


def test_strategy_summary_counts_only_ema_entries_after_cutoff(ledger):
    db, _ = ledger
    _close(db, "OLD/USDT:USDT", -9.0)
    old_entry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.conn.execute(
        "UPDATE trades SET entry_time=? WHERE symbol='OLD/USDT:USDT'", (old_entry,)
    )
    db.conn.commit()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _close(db, "BTC/USDT:USDT", 5.0)
    _close(db, "ETH/USDT:USDT", -2.0)
    _close(db, "SOL/USDT:USDT", 0.0)
    _close(db, "XRP/USDT:USDT", -7.0, strategy="utbot")
    db.log_trade_entry("HYPE/USDT:USDT", "short", 88.96, 2.72,
                       strategy=EMA200_UTBOT_RSI_STRATEGY)

    summary = db.get_strategy_trade_summary(EMA200_UTBOT_RSI_STRATEGY, cutoff)

    assert summary == {
        "entries": 4, "open": 1, "closed": 3, "wins": 1,
        "losses": 1, "flat": 1, "pnl_usdt": pytest.approx(3.0),
    }


def _controller(db, store):
    controller = emas.MainController.__new__(emas.MainController)
    controller.db = db
    controller.cfg = {}
    controller.get_active_trade_section = lambda: "signal_engine"
    controller.exchange = None

    async def balance():
        return 480.0, 300.0, 0.0

    controller.engines = {
        "signal": SimpleNamespace(trading_state_store=store, get_balance_info=balance)
    }
    return controller


def test_telegram_reset_flow_confirms_then_blocks_second_use(ledger, any_hour):
    db, store = ledger
    _close(db, "BTC/USDT:USDT", -3.0)
    controller = _controller(db, store)

    text, markup = controller._ema200_morning_reset_prompt()
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "e2h:mreset:do" in callbacks

    first = asyncio.run(controller._ema200_run_morning_reset())
    assert first.startswith("✅ 오전 진입초기화 완료")
    assert "연속손실 1회 → 0회" in first

    second = asyncio.run(controller._ema200_run_morning_reset())
    assert "불가" in second
    _, markup = controller._ema200_morning_reset_prompt()
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "e2h:mreset:do" not in callbacks


def test_session_status_panel_reports_since_reset_and_today(ledger, any_hour):
    db, store = ledger
    _close(db, "BTC/USDT:USDT", -3.0)
    perform_ema200_morning_entry_reset(db, store)
    _close(db, "ETH/USDT:USDT", 6.5)
    controller = _controller(db, store)

    text = asyncio.run(controller._ema200_session_status_text())

    assert "초기화 이후" in text
    assert "진입 1회 / 청산 1회" in text
    assert "실현손익 +6.5000 USDT" in text
    assert "📅 오늘" in text
    assert "진입 2회 / 청산 2회" in text
    assert "Equity 480.00 USDT (소액계좌)" in text
    assert "연속손실 0회 → 다음 진입 증거금 50%" in text


def test_main_keyboard_shows_ema_panel_and_hides_history_help():
    controller = emas.MainController.__new__(emas.MainController)
    keyboard = controller._build_main_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]

    assert "/emastatus" in labels
    assert "/emareset" in labels
    assert "/history" not in labels
    assert "/help" not in labels
    assert [b.text for b in keyboard.keyboard[2]] == ["/emastatus", "/emareset"]

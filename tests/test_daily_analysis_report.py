"""Daily 09:00-KST analysis report, decision journal and their wiring."""
import asyncio
import io
import json
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import emas
from bot_runtime import decision_journal
from bot_runtime.daily_analysis_report import (
    build_daily_analysis_report,
    collect_daily_report_inputs,
    consistency_checks,
    excursions,
    report_filename,
    report_window,
    scan_logs,
    ut_state_series,
)
from bot_runtime.database import DBManager
from bot_runtime.ema200_utbot_rsi import EMA200_UTBOT_RSI_STRATEGY
from trading_safety.order_state import SQLiteTradingStateStore

KST = decision_journal.KST
M15 = 900_000


@pytest.fixture
def journal_dir(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    monkeypatch.setenv(decision_journal.JOURNAL_DIR_ENV, str(path))
    return path


def _rows(n=420, start_ms=1_790_000_000_000):
    rows = []
    price = 100.0
    for i in range(n):
        close = max(1.0, price + 0.8 * math.sin(i / 7.0) + 0.1 * math.cos(i / 3.0))
        rows.append([start_ms + i * M15, price, max(price, close) + 0.1,
                     min(price, close) - 0.1, close, 1000.0])
        price = close
    return rows


def test_report_window_follows_0900_kst_trading_day():
    at_0830 = datetime(2026, 9, 29, 8, 30, tzinfo=KST)
    start, end, complete = report_window(at_0830)
    assert start == datetime(2026, 9, 28, 9, 0, tzinfo=KST)
    assert end == at_0830 and complete is False

    at_0930 = datetime(2026, 9, 29, 9, 30, tzinfo=KST)
    start, _, _ = report_window(at_0930)
    assert start == datetime(2026, 9, 29, 9, 0, tzinfo=KST)

    start, end, complete = report_window(at_0930, previous=True)
    assert (start, end, complete) == (
        datetime(2026, 9, 28, 9, 0, tzinfo=KST),
        datetime(2026, 9, 29, 9, 0, tzinfo=KST),
        True,
    )
    assert report_filename(start, end, True) == (
        "daily_analysis_20260928_0900_to_20260929_0900.txt"
    )


def test_ut_replay_matches_the_bot_ut_calculation():
    rows = _rows()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    params = {"UTBot": {"key_value": 1.0, "atr_period": 10, "use_heikin_ashi": False}}
    for i in range(330, len(rows), 7):
        window = rows[i - 300: i + 1]  # last row is the forming candle
        frame = pd.DataFrame(window, columns=["timestamp", "open", "high", "low", "close", "volume"])
        signal, _, detail = engine._calculate_utbot_signal(frame, params)
        replay = ut_state_series(window[:-1])[-1]
        assert replay["bias"] == detail["bias_side"]
        assert replay["signal"] == signal
        assert replay["trail"] == pytest.approx(detail["curr_stop"])


def test_excursions_are_side_aware():
    candles = [[0, 100, 104, 97, 101, 1], [1, 101, 106, 99, 100, 1]]
    long = excursions("long", 100.0, candles, leverage=5)
    short = excursions("short", 100.0, candles, leverage=5)
    assert long["mfe_price_pct"] == pytest.approx(6.0)
    assert long["mae_price_pct"] == pytest.approx(-3.0)
    assert short["mfe_price_pct"] == pytest.approx(3.0)
    assert short["mae_price_pct"] == pytest.approx(-6.0)
    assert short["mae_roe_pct"] == pytest.approx(-30.0)


def test_journal_writes_redacts_and_filters_by_window(journal_dir):
    token = "8751989898:AAHnUyY1bB9NocsBM11YVDvb71EofonFo-k"
    assert decision_journal.journal_event(
        "strategy", "entry_plan", symbol="BTC/USDT:USDT", note=f"token {token}",
        api_key="plain-secret", nested={"secret_key": "x", "rsi": 55.5},
    )
    now = datetime.now(timezone.utc)
    events = decision_journal.read_journal(now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert len(events) == 1
    raw = json.dumps(events[0])
    assert token not in raw and "plain-secret" not in raw
    assert events[0]["nested"]["rsi"] == 55.5
    assert decision_journal.read_journal(now + timedelta(minutes=1), now + timedelta(minutes=2)) == []


def test_journal_is_disabled_outside_the_official_launcher(monkeypatch):
    monkeypatch.delenv(decision_journal.JOURNAL_DIR_ENV, raising=False)
    monkeypatch.delenv("TRADINGBOT_OFFICIAL_LAUNCHER", raising=False)
    assert decision_journal.journal_event("strategy", "noop") is False


def test_scan_logs_groups_window_warnings_and_keeps_tracebacks(tmp_path):
    local = datetime.now().astimezone()
    inside = (local - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    outside = (local - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    log = tmp_path / "emas.log"
    log.write_text("\n".join([
        f"{outside},000 - ERROR - old failure 1",
        f"{inside},000 - INFO - Entry confirmed",
        f"{inside},100 - WARNING - Poll symbol BTC/USDT:USDT error: timeout 12",
        f"{inside},200 - WARNING - Poll symbol ETH/USDT:USDT error: timeout 15",
        f"{inside},300 - ERROR - Signal entry error: boom",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "RuntimeError: boom",
        f"{inside},400 - INFO - after",
    ]), encoding="utf-8")
    now = datetime.now(timezone.utc)
    result = scan_logs([log], now - timedelta(hours=1), now + timedelta(minutes=1))
    assert result["level_counts"] == {"INFO": 2, "WARNING": 2, "ERROR": 1}
    warning = next(g for g in result["groups"] if g["level"] == "WARNING")
    assert warning["count"] == 2
    assert result["tracebacks"][0]["lines"][-1] == "RuntimeError: boom"


def _event(event, ts, **fields):
    return {"ts": ts.isoformat(), "kst": ts.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "category": "strategy", "event": event, **fields}


def test_consistency_checks_flag_protection_and_exit_invariants():
    t0 = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
    journal = [
        _event("entry_plan", t0, symbol="BTC/USDT:USDT",
               risk_plan={"emergency_stop_required": True, "consecutive_losses": 3}),
        _event("entry_protection", t0 + timedelta(seconds=5), symbol="BTC/USDT:USDT",
               outcome="STRATEGY_MANAGED_NO_STOP", streak=3),
        _event("entry_gate_exit_tf", t0 + timedelta(minutes=1), symbol="ETH/USDT:USDT", allowed=False),
        _event("entry_filled", t0 + timedelta(minutes=2), symbol="ETH/USDT:USDT"),
    ] + [
        _event("exit_check", t0 + timedelta(minutes=15 * i), symbol="HYPE/USDT:USDT",
               side="short", ut_bias="long", decision="HOLD")
        for i in range(5)
    ]
    codes = {code for _, code, _ in consistency_checks({"journal": journal})}
    assert {
        "STOP_REQUIRED_NOT_PROTECTED",
        "NO_STOP_WITH_LOSS_STREAK",
        "EXIT_TF_GATE_BYPASSED",
        "OPPOSITE_UT_WITHOUT_FRESH_SIGNAL",
    } <= codes


def test_exit_checks_are_journaled_through_the_real_exit_path(journal_dir):
    from tests.test_ema200_exit_management import _harness

    harness = _harness()
    buy_idx = harness.first_fresh_buy_idx()
    harness.run(buy_idx + 2)

    now = datetime.now(timezone.utc)
    events = decision_journal.read_journal(now - timedelta(minutes=5), now + timedelta(minutes=1))
    checks = [e for e in events if e["event"] == "exit_check"]
    assert checks and checks[-1]["decision"] == "EXIT"
    assert all(c["decision"] == "HOLD" for c in checks[:-1])
    executed = [e for e in events if e["event"] == "exit_executed"]
    assert executed and executed[0]["reason"] == "EMA200_UTBOT_RSI_UT_BUY"


def _controller(tmp_path, rows):
    db = DBManager(str(tmp_path / "trades.db"))
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    controller = emas.MainController.__new__(emas.MainController)
    controller.db = db
    controller.cfg = SimpleNamespace(
        get=lambda key, default=None: {} if default is None else default,
        get_chat_id=lambda: 12345,
    )
    controller.get_active_trade_section = lambda: "signal_engine"
    controller.is_paused = False
    controller.exchange = SimpleNamespace(fetch_positions=lambda: [])
    controller.market_data_exchange = SimpleNamespace(
        fetch_ohlcv=lambda symbol, tf, since=None, limit=None: [list(r) for r in rows]
    )

    async def balance():
        return 480.0, 300.0, 0.0

    controller.engines = {"signal": SimpleNamespace(trading_state_store=store, get_balance_info=balance)}
    sent = []

    async def send_document(**kwargs):
        sent.append(kwargs)

    controller.tg_app = SimpleNamespace(bot=SimpleNamespace(send_document=send_document))
    return controller, db, store, sent


def test_report_end_to_end_contains_both_parts_and_is_sent(tmp_path, journal_dir):
    now = datetime.now(timezone.utc)
    rows = _rows(n=500, start_ms=int((now - timedelta(hours=100)).timestamp() * 1000))
    controller, db, store, sent = _controller(tmp_path, rows)
    decision_journal.journal_event(
        "strategy", "entry_plan", symbol="HYPE/USDT:USDT", side="short",
        risk_plan={"consecutive_losses": 0, "margin_percent": 50.0, "leverage": 5,
                   "emergency_stop_required": False, "emergency_exit_percent": 5.0},
        target_notional=1200.0, margin_to_use=240.0, sizing_equity=480.0,
    )
    decision_journal.journal_event(
        "operations", "entry_protection", symbol="HYPE/USDT:USDT", side="short",
        outcome="STRATEGY_MANAGED_NO_STOP", streak=0,
    )
    db.log_trade_entry("HYPE/USDT:USDT", "short", 88.96, 2.72, strategy=EMA200_UTBOT_RSI_STRATEGY)
    db.log_trade_close("HYPE/USDT:USDT", -3.1, -1.2, 90.1, "manual close")

    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    start, end, complete = report_window(now)
    inputs = asyncio.run(collect_daily_report_inputs(controller, start, end, log_paths=[]))
    text = build_daily_analysis_report(inputs)

    assert "PART 1. 매매전략 분석" in text and "PART 2. 자동매매 코드 작동 검증" in text
    assert "[거래 #1] HYPE/USDT:USDT SHORT" in text
    assert "리스크 계획: 연속손실 0회 → 증거금 50.0%" in text
    assert "보호주문 결과: STRATEGY_MANAGED_NO_STOP" in text
    assert "시장 재생(15m UT 재계산)" in text
    assert "manual close" in text
    assert "[2-7 코드 위치]" in text

    result = asyncio.run(controller._send_daily_analysis_report(now=now))
    assert sent and sent[0]["chat_id"] == 12345
    assert sent[0]["filename"] == result["filename"]
    assert sent[0]["filename"].endswith("_partial.txt")
    body = sent[0]["document"].getvalue().decode("utf-8")
    assert "PART 2. 자동매매 코드 작동 검증" in body
    assert "실현손익 -3.1000 USDT" in sent[0]["caption"]
    db.conn.close()
    store.close()


def test_scan_evaluations_are_reset_per_scan_and_journaled(journal_dir):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine._ema200_scan_evaluations = [
        {"symbol": "BTC/USDT:USDT", "signal": "long", "detail": {"rsi": 55.0}}
    ]
    candidate = {"symbol": "BTC/USDT:USDT", "side": "long", "score": 60.0, "rank": 1}
    engine._store_ema200_candidate_selection(
        enabled=True, candidates=[candidate], reason="test", closed_candle_ts=123,
    )
    engine._store_ema200_candidate_selection(
        enabled=True, candidates=[candidate], reason="test", closed_candle_ts=123,
    )
    now = datetime.now(timezone.utc)
    scans = [
        e for e in decision_journal.read_journal(now - timedelta(minutes=1), now + timedelta(minutes=1))
        if e["event"] == "ema200_scan"
    ]
    assert len(scans) == 1  # identical scans are de-duplicated
    assert scans[0]["evaluations"][0]["detail"]["rsi"] == 55.0


def test_duplicate_reduce_only_close_is_flagged_but_other_failures_too():
    audit = [
        {"client_order_id": "close-a", "symbol": "HBAR/USDT", "new_state": "FAILED",
         "detail": {"last_error": 'binance {"code":-2022,"msg":"ReduceOnly Order is rejected."}'}},
        {"client_order_id": "close-a", "symbol": "HBAR/USDT", "new_state": "CLOSED", "detail": {}},
        {"client_order_id": "entry-b", "symbol": "ETH/USDT", "new_state": "FAILED",
         "detail": {"last_error": "insufficient margin"}},
    ]
    findings = consistency_checks({"audit": audit})
    codes = [code for _, code, _ in findings]
    assert codes.count("DUPLICATE_REDUCE_ONLY_CLOSE") == 1
    assert codes.count("ORDER_FAILED") == 1


def test_log_paths_prefer_persistent_rotating_bot_log(tmp_path, monkeypatch):
    from bot_runtime import daily_analysis_report as report

    monkeypatch.setattr(report, "_ROOT", tmp_path)
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "emas.log"))
    (tmp_path / "emas.log").write_text("x")
    assert report.default_log_paths() == [tmp_path / "emas.log"]
    (tmp_path / "trading_bot.log.1").write_text("x")
    (tmp_path / "trading_bot.log").write_text("x")
    assert report.default_log_paths() == [
        tmp_path / "trading_bot.log.1",
        tmp_path / "trading_bot.log",
    ]


def test_market_replay_measures_excursions_on_1m_hold_only(tmp_path, journal_dir):
    now = datetime.now(timezone.utc)
    entry = now - timedelta(minutes=30)
    exit_at = now - timedelta(minutes=10)
    rows15 = _rows(n=500, start_ms=int((now - timedelta(hours=120)).timestamp() * 1000))

    def fetch(symbol, tf, since=None, limit=None):
        if tf == "1m":
            base = int(entry.timestamp() * 1000) // 60_000 * 60_000
            # A deep wick one minute before entry must not count as MAE.
            pre = [[base - 60_000, 100, 100, 50, 100, 1]]
            hold = [[base + i * 60_000, 100, 102, 99, 100, 1] for i in range(25)]
            return pre + hold
        return [list(r) for r in rows15]

    controller, db, store, _ = _controller(tmp_path, rows15)
    controller.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch)
    db.log_trade_entry("SOL/USDT:USDT", "long", 100.0, 1.0, strategy=EMA200_UTBOT_RSI_STRATEGY)
    db.conn.execute("UPDATE trades SET entry_time=?", (entry.isoformat(),))
    db.conn.commit()
    db.log_trade_close("SOL/USDT:USDT", 0.0, 0.0, 100.0, "test", exit_time=exit_at.isoformat())

    start, end, _ = report_window(now + timedelta(seconds=1))
    inputs = asyncio.run(collect_daily_report_inputs(controller, start, end, log_paths=[]))
    replay = next(iter(inputs["market_replay"].values()))
    assert replay["excursion_basis"] == "1m"
    assert replay["excursions"]["mae_price_pct"] == pytest.approx(-1.0)
    assert replay["excursions"]["mfe_price_pct"] == pytest.approx(2.0)
    db.conn.close()
    store.close()

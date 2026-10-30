"""Read-only status report / desktop monitor snapshot of the standalone strategies."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from btc_ema_pullback.ledger import StrategyLedger
from bot_runtime.standalone_snapshot import exchange_network, strategy_snapshots
from scripts import bot_status_readonly as status

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)   # 12:00 KST


def _seed(root, network="mainnet", mode="LIVE"):
    base = root / "runtime" / "btc_ma_cross"
    ledger = StrategyLedger(base / f"{network}_trades.sqlite3", base / f"{network}_events.jsonl")
    ledger.set_state(f"state:{mode}", {
        "trade": {"market_id": "SOLUSDT", "side": "SHORT", "quantity": "3", "entry_price": "150.5",
                  "stop_price": "", "lock_roi": "4", "lock_price": "149.3", "margin_used": "90"},
        "last_error": "",
    })
    for trade_id, exit_time, pnl in (("a", "2026-10-26T01:00:00+00:00", "-1.5"),
                                     ("b", "2026-10-27T01:00:00+00:00", "2.25")):
        ledger.upsert_trade({"trade_id": trade_id, "mode": mode, "network": network, "status": "CLOSED",
                             "entry_time": exit_time, "exit_time": exit_time, "net_pnl": pnl})
    ledger.event("SIGNAL", side="SHORT", market_id="SOLUSDT")
    ledger.event("PROFIT_LOCK", reached_roi=5, locked_roi=4, trigger="149.3")
    ledger.close()


def _config(**ma):
    return {"api": {"exchange_mode": "binance_mainnet", "mainnet": {"api_key": "K" * 64, "secret_key": "S" * 64}},
            "btc_ma_cross": {"enabled": True, "trading_mode": "LIVE", "scan_scope": "single",
                             "single_symbol": "SOLUSDT", "timeframe": "5m", **ma}}


def test_snapshot_reads_state_trades_and_events(tmp_path):
    _seed(tmp_path)
    rows = {r["key"]: r for r in strategy_snapshots(tmp_path, _config(), now=NOW)}
    ma = rows["btc_ma_cross"]
    assert ma["enabled"] and ma["mode"] == "LIVE" and ma["scope"] == "지정 SOLUSDT" and ma["timeframe"] == "5m"
    assert ma["trade"]["symbol"] == "SOLUSDT" and ma["trade"]["lock_roi"] == 4.0 and ma["trade"]["stop_price"] is None
    assert ma["today_trades"] == 1 and ma["today_pnl"] == 2.25
    assert ma["total_trades"] == 2 and ma["wins"] == 1 and ma["total_pnl"] == 0.75
    assert ma["recent"][0]["text"].startswith("PROFIT_LOCK: +5% 도달")
    pullback = rows["btc_ema_pullback"]
    assert pullback["enabled"] is False and pullback["trade"] is None and pullback["total_trades"] == 0
    json.dumps(rows)  # serialisable for the stream


def test_snapshot_never_writes(tmp_path):
    _seed(tmp_path)
    db = tmp_path / "runtime" / "btc_ma_cross" / "mainnet_trades.sqlite3"
    before = db.stat().st_mtime_ns, db.read_bytes()
    strategy_snapshots(tmp_path, _config(), now=NOW)
    assert (db.stat().st_mtime_ns, db.read_bytes()) == before
    # A missing ledger is reported as empty, not created.
    strategy_snapshots(tmp_path, {"api": {"exchange_mode": "binance_testnet"}}, now=NOW)
    assert not (tmp_path / "runtime" / "btc_ma_cross" / "testnet_trades.sqlite3").exists()


def test_upbit_mode_is_unsupported(tmp_path):
    rows = strategy_snapshots(tmp_path, {"api": {"exchange_mode": "upbit"}}, now=NOW)
    assert all(r["error"] for r in rows)
    assert exchange_network({"api": {"use_testnet": False}}) == "mainnet"


def test_redaction_hides_tokens_and_keys():
    line = ("ERROR telegram 1234567890:AAH" + "x" * 33 + " failed apiKey=abcd1234 secret: 'zzz' "
            "sig " + "f" * 64)
    out = status.redact(line)
    assert "AAH" not in out and "abcd1234" not in out and "zzz" not in out and "f" * 64 not in out
    assert out.count("[REDACTED]") >= 4


def test_report_renders_without_exchange_and_hides_keys(tmp_path, monkeypatch):
    _seed(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(_config()), encoding="utf-8")
    (tmp_path / "runtime" / "bot_heartbeat.json").write_text("{}", encoding="utf-8")
    log = tmp_path / "emas.log"
    log.write_text("INFO ok\nERROR boom token=SECRETVALUE\n", encoding="utf-8")
    monkeypatch.setenv("LOG_FILE", str(log))
    report = status.collect(tmp_path, with_exchange=False)
    text = status.render(report)
    assert "3/200 SMA 크로스 (/btcmacross): ON · LIVE · 지정 SOLUSDT · 5m" in text
    assert "보유: SOLUSDT SHORT" in text and "수익확보 +4% @ 149.3" in text
    assert "ERROR boom token=[REDACTED]" in text
    dumped = json.dumps(report, default=str) + text
    assert "K" * 64 not in dumped and "S" * 64 not in dumped and "SECRETVALUE" not in dumped


def test_gate_only_allows_read_only_commands():
    gate = (Path(__file__).resolve().parent.parent / "scripts" / "readonly_gate.sh").read_text(encoding="utf-8")
    assert "bot_status_readonly.py" in gate and "desktop_monitor_stream.py" in gate
    assert "exit 2" in gate
    for forbidden in ("bot_ctl", "restart", "manual_resume", "live_real_trade", "$SSH_ORIGINAL_COMMAND\n"):
        assert forbidden not in gate


def test_ledger_open_is_read_only(tmp_path):
    _seed(tmp_path)
    db = tmp_path / "runtime" / "btc_ma_cross" / "mainnet_trades.sqlite3"
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        try:
            conn.execute("DELETE FROM kv")
            raised = False
        except sqlite3.OperationalError:
            raised = True
    finally:
        conn.close()
    assert raised


def test_top_memory_processes_groups_by_name(tmp_path):
    from scripts import bot_status_readonly as status

    for pid, name, rss, swap in ((10, "python3", 400000, 100000), (11, "python3", 10240, 0), (12, "node", 204800, 512000)):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "status").write_text(f"Name:\t{name}\nVmRSS:\t{rss} kB\nVmSwap:\t{swap} kB\n", encoding="utf-8")
    (tmp_path / "self").mkdir()
    top = status.top_memory_processes(proc_root=str(tmp_path))
    assert [e["name"] for e in top] == ["node", "python3"]
    assert top[0]["swap_mb"] == 500.0
    assert top[1]["count"] == 2
    assert status.top_memory_processes(proc_root=str(tmp_path / "missing")) == []

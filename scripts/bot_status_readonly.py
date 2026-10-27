#!/usr/bin/env python3
"""Print a read-only status report of the running bot (for the read-only SSH key).

Reads the heartbeat, runtime snapshots, strategy ledgers and the log tail,
and queries the exchange with read-only calls (positions, balance, open
orders).  It never creates, cancels or changes orders or settings, and API
keys / tokens are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot_runtime.standalone_snapshot import exchange_network, strategy_snapshots  # noqa: E402

KST = timezone(timedelta(hours=9))
SECRET_PATTERNS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),                       # Telegram bot token
    re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|secret|token|signature|password|jwt)\b(['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"),
    re.compile(r"\b[A-Za-z0-9+_-]{40,}\b"),                                # long key-like strings (paths kept)
)
ERROR_MARKERS = ("ERROR", "CRITICAL", "Traceback", "Exception")


def redact(text):
    text = str(text)
    text = SECRET_PATTERNS[0].sub("[REDACTED]", text)
    text = SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    return SECRET_PATTERNS[2].sub("[REDACTED]", text)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def _kst(value):
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(float(value), timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment.astimezone(KST).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value or "-")


def process_status(root):
    heartbeat = root / "runtime" / "bot_heartbeat.json"
    info = {"heartbeat_age_sec": None, "launcher_processes": None}
    try:
        info["heartbeat_age_sec"] = round(time.time() - heartbeat.stat().st_mtime)
    except OSError:
        pass
    try:
        out = subprocess.run(["pgrep", "-f", "scripts/launch_emas.py"], capture_output=True, text=True, timeout=5)
        info["launcher_processes"] = len([line for line in out.stdout.split() if line.strip()])
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def main_engine_status(root):
    runtime = _load_json(root / "runtime" / "desktop_monitor.json", {})
    if not isinstance(runtime, dict):
        return {}
    bot = runtime.get("bot") if isinstance(runtime.get("bot"), dict) else {}
    rows = [r for r in runtime.get("status_rows", []) if isinstance(r, dict)]
    first = rows[0] if rows else {}
    return {
        "updated_at": runtime.get("updated_at"),
        "paused": bot.get("paused"),
        "active_strategy": bot.get("active_strategy"),
        "symbol": first.get("symbol"),
        "decision": first.get("entry_reason_ko") or first.get("side"),
        "equity": first.get("equity"),
        "free_usdt": first.get("free_usdt"),
        "daily_pnl": first.get("daily_pnl"),
    }


def exchange_status(config):
    """Balance, positions and protective orders via read-only exchange calls."""
    from scripts.desktop_monitor_stream import build_exchanges, fetch_orders, fetch_positions

    mode, private, _public = build_exchanges(config)
    private.load_markets()
    result = {"mode": mode, "balance": None, "positions": [], "error": None}
    try:
        balance = private.fetch_balance()
        usdt = balance.get("USDT") if isinstance(balance.get("USDT"), dict) else {}
        result["balance"] = {"total": usdt.get("total"), "free": usdt.get("free")}
    except Exception as exc:
        result["error"] = f"balance: {type(exc).__name__}"
    for position in fetch_positions(private):
        symbol = position.get("symbol") or ""
        try:
            orders = fetch_orders(private, f"{symbol}:USDT" if symbol.endswith("/USDT") else symbol)
        except Exception as exc:
            orders = [{"type": f"조회 실패 {type(exc).__name__}"}]
        result["positions"].append({**position, "orders": orders})
    return result


def log_errors(limit=8):
    path = Path(os.environ.get("LOG_FILE") or Path.home() / "emas.log")
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 300_000))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    hits = [line for line in lines if any(marker in line for marker in ERROR_MARKERS)]
    return [redact(line)[:300] for line in hits[-limit:]]


def collect(root, *, with_exchange=True):
    config = _load_json(root / "config.json", {})
    report = {
        "generated_at": time.time(),
        "network": exchange_network(config) if config else None,
        "process": process_status(root),
        "main_engine": main_engine_status(root),
        "strategies": strategy_snapshots(root, config) if config else [],
        "exchange": None,
        "log_errors": log_errors(),
    }
    if with_exchange and config and report["network"]:
        try:
            report["exchange"] = exchange_status(config)
        except Exception as exc:
            report["exchange"] = {"error": f"{type(exc).__name__}: {redact(exc)}"[:300]}
    return report


def _fmt(value, digits=2, sign=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:+.{digits}f}" if sign else f"{number:.{digits}f}"


def render(report):
    lines = [f"🤖 봇 상태 ({_kst(report['generated_at'])} KST)"]
    proc = report["process"]
    age = proc.get("heartbeat_age_sec")
    alive = age is not None and age <= 180 and proc.get("launcher_processes") == 1
    lines.append(
        f"프로세스: {'정상' if alive else '확인 필요'} · 실행 {proc.get('launcher_processes')}개 · 하트비트 {age}초 전"
    )
    lines.append(f"거래소: {report.get('network') or '업비트/알 수 없음'} (/setup)")
    main = report["main_engine"]
    if main:
        lines.append(
            f"메인 선물: {'일시정지' if main.get('paused') else '동작 중'} · 활성 {main.get('active_strategy') or '-'} · "
            f"계좌 {_fmt(main.get('equity'))} / 가용 {_fmt(main.get('free_usdt'))} USDT · 오늘 {_fmt(main.get('daily_pnl'), sign=True)}"
        )
    exchange = report.get("exchange") or {}
    if exchange:
        lines.append("")
        balance = exchange.get("balance") or {}
        lines.append(f"💰 거래소 잔고: {_fmt(balance.get('total'))} USDT (가용 {_fmt(balance.get('free'))})")
        for pos in exchange.get("positions", []):
            lines.append(
                f"  {pos.get('symbol')} {pos.get('side')} {pos.get('contracts')} @ {pos.get('entry_price')} · "
                f"현재 {pos.get('mark_price')} · {_fmt(pos.get('leverage'), 0)}x · "
                f"PnL {_fmt(pos.get('unrealized_pnl'), 4, True)} ({_fmt(pos.get('roe_percent'), 2, True)}%)"
            )
            for order in pos.get("orders", []):
                lines.append(f"    주문 {order.get('type')} {order.get('side') or ''} @ {order.get('price')}")
        if not exchange.get("positions"):
            lines.append("  보유 포지션 없음")
        if exchange.get("error"):
            lines.append(f"  거래소 조회 오류: {exchange['error']}")
    for item in report["strategies"]:
        lines.append("")
        lines.append(
            f"📏 {item['label']}: {'ON' if item['enabled'] else 'OFF'} · {item['mode']} · {item['scope']} · {item['timeframe']}"
            + (f" · {item['entry_mode']}" if item.get("entry_mode") else "")
        )
        if item.get("error"):
            lines.append(f"  {item['error']}")
            continue
        trade = item.get("trade")
        if trade:
            lock = f"+{trade['lock_roi']:g}% @ {trade['lock_price']}" if trade.get("lock_price") else "아직 없음"
            lines.append(
                f"  보유: {trade['symbol']} {trade['side']} {trade['quantity']} @ {trade['entry_price']} · "
                f"비상손절 {trade.get('stop_price') or 'OFF'} · 수익확보 {lock}"
            )
        else:
            lines.append("  보유: 없음")
        lines.append(
            f"  오늘 {item['today_trades']}회 {_fmt(item['today_pnl'], 4, True)} · 누적 {item['total_trades']}회 "
            f"(승 {item['wins']}) {_fmt(item['total_pnl'], 4, True)} USDT"
        )
        if item.get("last_error"):
            lines.append(f"  최근 오류: {redact(item['last_error'])}")
        for event in item.get("recent", [])[:5]:
            lines.append(f"  {_kst(event.get('ts'))} {redact(event.get('text'))}")
    if report["log_errors"]:
        lines.append("")
        lines.append("⚠️ 최근 로그 오류")
        lines.extend(f"  {line}" for line in report["log_errors"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-exchange", action="store_true")
    args = parser.parse_args(argv)
    report = collect(Path(args.root).resolve(), with_exchange=not args.no_exchange)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

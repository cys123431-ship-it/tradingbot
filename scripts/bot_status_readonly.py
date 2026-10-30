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


def _read_proc_text(path_str):
    try:
        with open(path_str, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _proc_rss_kb(pid):
    text = _read_proc_text(f"/proc/{pid}/status")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _proc_threads(pid):
    text = _read_proc_text(f"/proc/{pid}/status")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("Threads:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _proc_cpu_ticks(pid):
    text = _read_proc_text(f"/proc/{pid}/stat")
    if not text:
        return None
    # Format: pid (comm) state ppid ... utime(14) stime(15) ...
    # comm may contain spaces/parens; strip through last ')'.
    try:
        rparen = text.rfind(")")
        if rparen < 0:
            return None
        rest = text[rparen + 2:].split()
        # rest[0] is field 3 (state); utime is field 14 -> rest[11], stime 15 -> rest[12]
        utime = int(rest[11])
        stime = int(rest[12])
        return utime + stime
    except (IndexError, ValueError):
        return None


def top_memory_processes(limit=5, proc_root="/proc"):
    """Largest memory users grouped by process name (RAM + swap, MB). Names only, no args."""
    totals = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    for pid in entries:
        if not pid.isdigit():
            continue
        fields = {}
        try:
            with open(os.path.join(proc_root, pid, "status"), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    key, _, value = line.partition(":")
                    if key in ("Name", "VmRSS", "VmSwap"):
                        fields[key] = value.strip()
        except OSError:
            continue

        def _mb(key):
            try:
                return int(fields.get(key, "0 kB").split()[0]) / 1024.0
            except (ValueError, IndexError):
                return 0.0

        name = fields.get("Name") or "?"
        entry = totals.setdefault(name, {"name": name, "count": 0, "rss_mb": 0.0, "swap_mb": 0.0})
        entry["count"] += 1
        entry["rss_mb"] += _mb("VmRSS")
        entry["swap_mb"] += _mb("VmSwap")
    ranked = sorted(totals.values(), key=lambda e: e["rss_mb"] + e["swap_mb"], reverse=True)
    return [
        {**e, "rss_mb": round(e["rss_mb"], 1), "swap_mb": round(e["swap_mb"], 1)}
        for e in ranked[:limit]
        if e["rss_mb"] + e["swap_mb"] > 0
    ]


def system_status(root):
    info = {
        "load_avg": None,
        "cpu_count": None,
        "mem_total_mb": None,
        "mem_available_mb": None,
        "swap_total_mb": None,
        "swap_free_mb": None,
        "bot_pid": None,
        "bot_rss_mb": None,
        "bot_cpu_percent": None,
        "bot_threads": None,
        "watchdog": None,
    }
    try:
        info["cpu_count"] = os.cpu_count()
    except Exception:
        pass

    # load avg
    try:
        text = _read_proc_text("/proc/loadavg")
        if text:
            parts = text.split()
            info["load_avg"] = [float(x) for x in parts[:3]]
    except Exception:
        pass

    # Memory
    try:
        text = _read_proc_text("/proc/meminfo")
        if text:
            vals = {}
            for line in text.splitlines():
                if ":" in line:
                    key, rest = line.split(":", 1)
                    vals[key.strip()] = rest.strip()
            def _kb_mb(key):
                v = vals.get(key)
                if not v:
                    return None
                try:
                    return int(int(v.split()[0]) / 1024)
                except (IndexError, ValueError):
                    return None
            info["mem_total_mb"] = _kb_mb("MemTotal")
            info["mem_available_mb"] = _kb_mb("MemAvailable")
            info["swap_total_mb"] = _kb_mb("SwapTotal")
            info["swap_free_mb"] = _kb_mb("SwapFree")
    except Exception:
        pass

    # Bot pid: pick the pgrep emas match with the largest RSS.
    pid = None
    try:
        out = subprocess.run(
            ["pgrep", "-f", "emas"], capture_output=True, text=True, timeout=5
        )
        candidates = []
        for token in out.stdout.split():
            try:
                p = int(token.strip())
            except ValueError:
                continue
            rss = _proc_rss_kb(p)
            if rss is not None:
                candidates.append((rss, p))
        if candidates:
            candidates.sort(reverse=True)
            pid = candidates[0][1]
    except (OSError, subprocess.SubprocessError):
        pass

    info["bot_pid"] = pid
    if pid is not None:
        try:
            rss_kb = _proc_rss_kb(pid)
            if rss_kb is not None:
                info["bot_rss_mb"] = round(rss_kb / 1024.0, 1)
        except Exception:
            pass
        try:
            info["bot_threads"] = _proc_threads(pid)
        except Exception:
            pass
        try:
            t1 = _proc_cpu_ticks(pid)
            if t1 is not None:
                time.sleep(1.0)
                t2 = _proc_cpu_ticks(pid)
                if t2 is not None:
                    try:
                        hz = os.sysconf("SC_CLK_TCK")
                    except (ValueError, OSError):
                        hz = 100
                    if hz:
                        info["bot_cpu_percent"] = round(
                            (t2 - t1) / float(hz) * 100.0, 1
                        )
        except Exception:
            pass

    info["top_memory"] = top_memory_processes()

    # Watchdog stats
    try:
        info["watchdog"] = _load_json(root / "runtime" / "loop_watchdog.json", None)
    except Exception:
        info["watchdog"] = None

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


def _log_lines(max_bytes=300_000):
    path = Path(os.environ.get("LOG_FILE") or Path.home() / "emas.log")
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - max_bytes))
            return handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def log_tail(lines=250):
    """Last log lines plus every recent traceback in full, secrets redacted."""
    rows = _log_lines(2_000_000)
    blocks, i = [], 0
    while i < len(rows):
        if rows[i].startswith("Traceback"):
            start = max(0, i - 2)
            j = i + 1
            while j < len(rows) and (rows[j].startswith((" ", "\t")) or not rows[j].strip()):
                j += 1
            blocks.append(rows[start:j + 1])
            i = j
        i += 1
    out = ["===== 최근 traceback (최대 6개) ====="]
    for block in blocks[-6:]:
        out.extend(block)
        out.append("-----")
    out.append(f"===== 로그 마지막 {lines}줄 =====")
    out.extend(rows[-lines:])
    return [redact(line)[:500] for line in out]


def log_errors(limit=8):
    lines = _log_lines()
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
        "system": system_status(root),
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
    sysinfo = report.get("system") or {}
    load = sysinfo.get("load_avg")
    if load and len(load) >= 3:
        load_txt = f"{load[0]:.2f}/{load[1]:.2f}/{load[2]:.2f}"
    else:
        load_txt = "-"
    cpu_count_txt = sysinfo.get("cpu_count")
    cpu_count_txt = "-" if cpu_count_txt is None else str(cpu_count_txt)
    avail = sysinfo.get("mem_available_mb")
    avail = "-" if avail is None else str(avail)
    total = sysinfo.get("mem_total_mb")
    total = "-" if total is None else str(total)
    bot_cpu = sysinfo.get("bot_cpu_percent")
    bot_cpu = "-" if bot_cpu is None else f"{bot_cpu:.1f}"
    rss = sysinfo.get("bot_rss_mb")
    rss = "-" if rss is None else f"{rss:.1f}"
    swap_total = sysinfo.get("swap_total_mb")
    swap_free = sysinfo.get("swap_free_mb")
    swap_txt = "-" if swap_total is None else (f"{swap_total - (swap_free or 0)}/{swap_total} MB" if swap_total else "없음")
    threads = sysinfo.get("bot_threads")
    threads = "-" if threads is None else str(threads)
    lines.append(
        f"서버: 부하 {load_txt} (CPU {cpu_count_txt}개) · 메모리 가용 {avail}/{total} MB · 스왑 {swap_txt} · 봇 CPU {bot_cpu}% · 메모리 {rss} MB · 스레드 {threads}"
    )
    top = sysinfo.get("top_memory") or []
    if top:
        lines.append("메모리 상위: " + " · ".join(
            f"{redact(e['name'])}×{e['count']} {e['rss_mb']:.0f}+스왑{e['swap_mb']:.0f}MB" for e in top
        ))
    wd = sysinfo.get("watchdog")
    if isinstance(wd, dict):
        blocks_total = wd.get("blocks_total")
        max_block = wd.get("max_block_sec")
        last_block = wd.get("last_block_sec")
        last_at = wd.get("last_block_at")
        try:
            max_txt = f"{float(max_block):.1f}"
        except (TypeError, ValueError):
            max_txt = "-"
        if last_block is None:
            last_txt = "-"
        else:
            try:
                last_txt = f"{float(last_block):.1f}"
            except (TypeError, ValueError):
                last_txt = "-"
        if last_at is None:
            at_txt = "-"
        else:
            try:
                at_txt = _kst(last_at)
            except Exception:
                at_txt = "-"
        lines.append(
            f"이벤트 루프 멈춤: {blocks_total}회 · 최대 {max_txt}s · 최근 {last_txt}s ({at_txt})"
        )
        stack_top = wd.get("last_block_stack_top")
        if isinstance(stack_top, list):
            for st_line in stack_top:
                try:
                    lines.append("    " + redact(str(st_line)))
                except Exception:
                    lines.append("    " + str(st_line))
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
    parser.add_argument("--logs", action="store_true")
    args = parser.parse_args(argv)
    if args.logs:
        print("\n".join(log_tail()))
        return 0
    report = collect(Path(args.root).resolve(), with_exchange=not args.no_exchange)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

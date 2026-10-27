"""Read-only snapshot of the standalone futures strategies (3/200 SMA, BTC pullback).

Used by the desktop monitor stream and the read-only status script.  It only
opens the strategy ledgers with SQLite ``mode=ro`` and tails the event logs;
it never writes state, places orders or touches the exchange.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from btc_ema_pullback.config import normalize_btc_pullback_config
from btc_ema_pullback.ledger import trading_day_bounds
from btc_ma_cross.config import normalize_btc_ma_cross_config

STRATEGIES = (
    # (config key, runtime subdir, normalizer)
    ("btc_ma_cross", "btc_ma_cross", normalize_btc_ma_cross_config),
    ("btc_ema_pullback", "btc_ema_pullback", normalize_btc_pullback_config),
)
DECISION_EVENTS = {"SIGNAL", "SKIP", "ORDER", "EXIT", "FAILSAFE", "PROFIT_LOCK", "ERROR"}


def exchange_network(config):
    """'mainnet' / 'testnet' from the bot's /setup exchange; None for Upbit."""
    api = config.get("api") if isinstance(config.get("api"), dict) else {}
    mode = str(api.get("exchange_mode") or "").lower()
    if mode == "binance_mainnet":
        return "mainnet"
    if mode == "binance_testnet":
        return "testnet"
    if mode:
        return None
    return "testnet" if api.get("use_testnet", True) else "mainnet"


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _read_db(path, mode, day_start, day_end):
    """(state, today's closed trades, all closed trades) without writing anything."""
    if not path.is_file():
        return {}, [], []
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (f"state:{mode}",)).fetchone()
        try:
            state = json.loads(row["value"]) if row else {}
        except (TypeError, ValueError):
            state = {}
        trades = [dict(r) for r in conn.execute(
            "SELECT exit_time, side, net_pnl, exit_reason, extra FROM trades "
            "WHERE mode=? AND status='CLOSED' ORDER BY exit_time", (mode,))]
    finally:
        conn.close()
    today = [t for t in trades if day_start.isoformat() <= str(t.get("exit_time") or "") < day_end.isoformat()]
    return (state if isinstance(state, dict) else {}), today, trades


def _recent_events(path, limit):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 400_000))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") in DECISION_EVENTS:
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def _event_text(event):
    kind = event.get("event")
    if kind == "SIGNAL":
        side = event.get("side")
        detail = f"{event.get('market_id') or ''} {side} 신호".strip() if side else (event.get("skip_reason") or "-")
    elif kind == "ORDER":
        detail = f"{event.get('symbol') or ''} {event.get('side')} {event.get('quantity')} @ {event.get('actual_avg_entry')}"
    elif kind == "PROFIT_LOCK":
        detail = f"+{event.get('reached_roi')}% 도달 → +{event.get('locked_roi')}% 확보 @ {event.get('trigger')}"
    else:
        detail = event.get("skip_reason") or event.get("exit_reason") or event.get("reason") or event.get("error") or "-"
    return f"{kind}: {str(detail)[:200]}"


def _scope_label(cfg):
    scope = cfg.get("scan_scope")
    if scope == "alts":
        return f"알트 상위{cfg.get('alt_universe_size', 50)}"
    if scope == "single":
        return f"지정 {cfg.get('single_symbol')}"
    return cfg.get("symbol") or "BTCUSDT"


def strategy_snapshots(root, config, *, now=None, events=5):
    """One dict per standalone strategy, JSON-serialisable and secret-free."""
    root = Path(root)
    network = exchange_network(config)
    now = now or datetime.now(timezone.utc)
    rows = []
    for key, subdir, normalize in STRATEGIES:
        cfg = normalize(config.get(key) if isinstance(config.get(key), dict) else {})
        label = f"3/{cfg['slow_period']} SMA 크로스" if key == "btc_ma_cross" else "BTC 눌림목"
        mode = "LIVE" if cfg.get("trading_mode") == "LIVE" else "DRY_RUN"
        item = {
            "key": key,
            "label": label,
            "enabled": bool(cfg.get("enabled")),
            "mode": mode,
            "network": network,
            "scope": _scope_label(cfg),
            "timeframe": cfg.get("timeframe") or "15m",
            "entry_mode": cfg.get("entry_mode"),
            "trade": None,
            "today_trades": 0,
            "today_pnl": 0.0,
            "total_trades": 0,
            "total_pnl": 0.0,
            "wins": 0,
            "last_error": "",
            "recent": [],
            "error": None,
        }
        if network is None:
            item["error"] = "/setup 거래소가 업비트라 사용 안 함"
            rows.append(item)
            continue
        base = root / "runtime" / subdir
        start, end, _ = trading_day_bounds(now, cfg.get("day_timezone") or "Asia/Seoul")
        try:
            state, today, trades = _read_db(base / f"{network}_trades.sqlite3", mode, start, end)
        except sqlite3.Error as exc:
            item["error"] = f"상태 DB 읽기 실패: {exc}"
            rows.append(item)
            continue
        trade = state.get("trade") if isinstance(state.get("trade"), dict) else None
        if trade:
            item["trade"] = {
                "symbol": trade.get("market_id") or "BTCUSDT",
                "side": trade.get("side"),
                "quantity": _num(trade.get("quantity")),
                "entry_price": _num(trade.get("entry_price")),
                "stop_price": _num(trade.get("stop_price")),
                "lock_roi": _num(trade.get("lock_roi")),
                "lock_price": _num(trade.get("lock_price")),
                "best_price": _num(trade.get("best_price")),
                "margin_used": _num(trade.get("margin_used")),
                "leverage": _num(trade.get("leverage")),
                "entry_time": trade.get("entry_time"),
            }
        pnl = [_num(t.get("net_pnl")) or 0.0 for t in trades]
        item.update(
            today_trades=len(today),
            today_pnl=round(sum(_num(t.get("net_pnl")) or 0.0 for t in today), 6),
            total_trades=len(trades),
            total_pnl=round(sum(pnl), 6),
            wins=sum(1 for value in pnl if value > 0),
            last_error=str(state.get("last_error") or "")[:300],
            recent=[
                {"ts": e.get("ts"), "text": _event_text(e)}
                for e in _recent_events(base / f"{network}_events.jsonl", events)
            ],
        )
        rows.append(item)
    return rows


__all__ = ("exchange_network", "strategy_snapshots")

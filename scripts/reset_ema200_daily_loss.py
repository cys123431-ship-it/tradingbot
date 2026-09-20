#!/usr/bin/env python3
"""Reset only today's EMA200 daily entry-loss baseline, preserving history."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot_runtime.database import DBManager
from bot_runtime.ema200_utbot_rsi import (
    EMA200_DAILY_LOSS_RESET_STATE_KEY,
    ema200_kst_date,
)
from trading_safety.order_state import (
    DAILY_LOSS_ENTRY_LOCK_KEY,
    SQLiteTradingStateStore,
    utc_now_iso,
)


def reset_ema200_daily_loss(
    db,
    store,
    *,
    reason="operator_requested",
    force=False,
):
    today = ema200_kst_date()
    existing = store.get_runtime_state(EMA200_DAILY_LOSS_RESET_STATE_KEY)
    if (
        not force
        and isinstance(existing, dict)
        and str(existing.get("date") or "") == today
    ):
        cleared_common_lock = store.delete_runtime_state(
            DAILY_LOSS_ENTRY_LOCK_KEY
        )
        return {
            **existing,
            "already_reset": True,
            "cleared_common_daily_lock": bool(cleared_common_lock),
        }
    trade_count, daily_pnl = db.get_daily_stats()
    payload = {
        "date": today,
        "baseline_realized_pnl": float(daily_pnl or 0.0),
        "trade_count_at_reset": int(trade_count or 0),
        "reset_at": utc_now_iso(),
        "reason": str(reason or "operator_requested"),
    }
    store.set_runtime_state(EMA200_DAILY_LOSS_RESET_STATE_KEY, payload)
    cleared_common_lock = store.delete_runtime_state(DAILY_LOSS_ENTRY_LOCK_KEY)
    return {
        **payload,
        "already_reset": False,
        "cleared_common_daily_lock": bool(cleared_common_lock),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    config_path = Path(os.getenv("TRADINGBOT_CONFIG", ROOT / "config.json"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    db_path = Path(config.get("logging", {}).get("db_path", "bot_database.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    state_path = Path(
        os.getenv(
            "CRYPTO_TRADING_STATE_DB",
            str(ROOT / "runtime" / "trading_state.sqlite3"),
        )
    )
    db = DBManager(str(db_path))
    store = SQLiteTradingStateStore(state_path)
    db.trade_result_store = store
    try:
        result = reset_ema200_daily_loss(db, store, force=args.force)
        if args.quiet:
            action = "already-applied" if result["already_reset"] else "applied"
            print(
                "EMA200 daily loss baseline reset: "
                f"{action} date={result['date']}"
            )
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        db.conn.close()
        store.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Reset the EMA200 loss-streak stage without deleting trade history."""

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
    EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY,
    EMA200_UTBOT_RSI_STRATEGY,
)
from trading_safety.order_state import SQLiteTradingStateStore, utc_now_iso


def reset_ema200_consecutive_losses(db, store, *, reason="operator_requested"):
    raw_streak = db.get_consecutive_strategy_losses(
        EMA200_UTBOT_RSI_STRATEGY
    )
    payload = {
        "reset_at": utc_now_iso(),
        "raw_consecutive_losses_at_reset": int(raw_streak),
        "reason": str(reason or "operator_requested"),
    }
    store.set_runtime_state(
        EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY,
        payload,
    )
    return payload


def main():
    parser = argparse.ArgumentParser()
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
        result = reset_ema200_consecutive_losses(db, store)
        if args.quiet:
            print("EMA200 consecutive-loss stage reset: applied")
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        db.conn.close()
        store.close()


if __name__ == "__main__":
    main()

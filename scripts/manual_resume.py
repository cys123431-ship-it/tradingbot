import argparse
from pathlib import Path
import json
import shutil
from datetime import datetime, timezone

PAUSE_STATE_FILE = Path("runtime/critical_pause_state.json")
ARCHIVE_DIR = Path("runtime/pause_archive")

CONFIRM = "I_CONFIRM_MANUAL_RISK_CHECK_DONE"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != CONFIRM:
        raise SystemExit("Manual confirmation text mismatch: exact verification token missing.")

    if not PAUSE_STATE_FILE.exists():
        print("NO_PAUSE_STATE")
        return

    try:
        with PAUSE_STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as read_err:
        raise SystemExit(f"Error reading pause state file: {read_err}")

    if args.symbol not in {state.get("symbol"), "*"}:
        raise SystemExit(f"Symbol {args.symbol} does not match pause state symbol: {state.get('symbol')}")

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = args.symbol.replace("/", "_").replace(":", "_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = ARCHIVE_DIR / f"critical_pause_{safe_symbol}_{ts}.json"

    try:
        shutil.move(str(PAUSE_STATE_FILE), str(archive))
        print(f"RESUMED {args.symbol}; archived={archive}")
    except Exception as move_err:
        raise SystemExit(f"Failed to clear/archive pause state: {move_err}")

if __name__ == "__main__":
    main()

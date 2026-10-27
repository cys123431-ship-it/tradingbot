#!/usr/bin/env bash
# Forced command for the read-only SSH key (authorized_keys: command="bash .../readonly_gate.sh").
# Only the status report and the desktop monitor stream can run; both are read-only.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$APP_DIR/venv/bin/python"
cd "$APP_DIR"

case "${SSH_ORIGINAL_COMMAND:-status}" in
  status)
    exec "$PYTHON_BIN" scripts/bot_status_readonly.py
    ;;
  "status --json")
    exec "$PYTHON_BIN" scripts/bot_status_readonly.py --json
    ;;
  *desktop_monitor_stream.py*)
    exec "$PYTHON_BIN" scripts/desktop_monitor_stream.py --interval 2 --timeframe 1m --candles 240
    ;;
  *)
    echo "read-only key: only 'status', 'status --json' and the desktop monitor stream are allowed" >&2
    exit 2
    ;;
esac

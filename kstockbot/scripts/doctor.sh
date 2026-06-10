#!/bin/bash
set -e

APP_DIR="${APP_DIR:-/home/azureuser/kstockbot}"
PARENT_DIR="${PARENT_DIR:-/home/azureuser}"
VENV_DIR="$APP_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

echo "===== KStockBot Doctor ====="
echo "APP_DIR=$APP_DIR"

if [ ! -d "$APP_DIR" ]; then
  echo "ERROR: APP_DIR not found: $APP_DIR"
  exit 1
fi

cd "$APP_DIR"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "WARNING: venv python not found. Falling back to python3."
  PYTHON_BIN="python3"
fi

if [ -f "$APP_DIR/.env" ]; then
  set -a
  source "$APP_DIR/.env"
  set +a
else
  echo "WARNING: .env not found."
fi

KSTOCK_HOST="${KSTOCK_HOST:-127.0.0.1}"
KSTOCK_PORT="${KSTOCK_PORT:-8090}"

echo "===== Sanitized ENV Check ====="
echo "KSTOCK_MODE=${KSTOCK_MODE:-unset}"
echo "KSTOCK_KIS_IS_MOCK=${KSTOCK_KIS_IS_MOCK:-unset}"
echo "KSTOCK_WEBHOOK_FAST_ACK=${KSTOCK_WEBHOOK_FAST_ACK:-unset}"
echo "KSTOCK_HOST=$KSTOCK_HOST"
echo "KSTOCK_PORT=$KSTOCK_PORT"
echo "KIS_APP_KEY_SET=$([ -n "${KSTOCK_KIS_APP_KEY:-}" ] && echo yes || echo no)"
echo "KIS_APP_SECRET_SET=$([ -n "${KSTOCK_KIS_APP_SECRET:-}" ] && echo yes || echo no)"
echo "KIS_ACCOUNT_SET=$([ -n "${KSTOCK_KIS_ACCOUNT_NO:-}" ] && echo yes || echo no)"
echo "TELEGRAM_TOKEN_SET=$([ -n "${KSTOCK_TELEGRAM_BOT_TOKEN:-}" ] && echo yes || echo no)"
echo "TELEGRAM_OWNER_SET=$([ -n "${KSTOCK_TELEGRAM_OWNER_ID:-}" ] && echo yes || echo no)"
echo "WEBHOOK_SECRET_SET=$([ -n "${KSTOCK_WEBHOOK_SECRET:-}" ] && [ "${KSTOCK_WEBHOOK_SECRET:-}" != "change-me" ] && echo yes || echo no)"

echo "===== Python Version ====="
"$PYTHON_BIN" --version

echo "===== Dependency Import Check ====="
PYTHONPATH="$PARENT_DIR" "$PYTHON_BIN" - <<'PY'
mods = ["fastapi", "uvicorn", "httpx", "apscheduler", "telegram", "psutil", "pydantic"]
for m in mods:
    try:
        __import__(m)
        print(f"{m}: ok")
    except Exception as e:
        print(f"{m}: FAIL {e}")
PY

echo "===== pip check ====="
"$PYTHON_BIN" -m pip check || echo "pip check reported issues"

echo "===== Script Check ====="
for f in "$APP_DIR/scripts/kstock_ctl.sh" "$APP_DIR/scripts/install_service.sh" "$APP_DIR/scripts/doctor.sh" "$APP_DIR/scripts/webhook_smoke.sh"; do
  if [ -x "$f" ]; then
    echo "$(basename "$f"): executable"
  elif [ -f "$f" ]; then
    echo "$(basename "$f"): found but not executable"
  else
    echo "$(basename "$f"): missing"
  fi
done

echo "===== Workflow Check ====="
if [ -f "$PARENT_DIR/.github/workflows/deploy-kstockbot.yml" ]; then
  echo "deploy-kstockbot.yml: found"
else
  echo "deploy-kstockbot.yml: not found at $PARENT_DIR/.github/workflows/deploy-kstockbot.yml"
fi

echo "===== py_compile ====="
PYTHONPATH="$PARENT_DIR" "$PYTHON_BIN" -m py_compile app/*.py

echo "===== pytest ====="
PYTHONPATH="$PARENT_DIR" "$PYTHON_BIN" -m pytest -q

echo "===== Process ====="
ps -eo pid,ppid,stat,etime,%mem,%cpu,cmd | grep -E "kstockbot|uvicorn" | grep -v grep || true

echo "===== Health ====="
curl -fsS "http://127.0.0.1:${KSTOCK_PORT}/health" || echo "health failed"

echo
echo "===== Status ====="
curl -fsS "http://127.0.0.1:${KSTOCK_PORT}/status" || echo "status failed"

echo
echo "===== Runtime/Cache File Check ====="
find "$APP_DIR" -maxdepth 3 \( -name "__pycache__" -o -name ".pytest_cache" -o -name "*.pyc" \) -print || true
find "$APP_DIR/runtime" -maxdepth 1 -type f ! -name ".gitkeep" -print 2>/dev/null || true
find "$APP_DIR/logs" -maxdepth 1 -type f ! -name ".gitkeep" -print 2>/dev/null || true

echo
echo "===== Webhook Event Ledger ====="
if [ -f "$APP_DIR/runtime/webhook_events.json" ]; then
  echo "webhook_events.json: found"
  "$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path

path = Path("runtime/webhook_events.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"events_count={len(data) if isinstance(data, list) else 'invalid'}")
    if isinstance(data, list) and data:
        last = data[-1]
        safe = {
            "event_id": last.get("event_id"),
            "status": last.get("status"),
            "action": last.get("action"),
            "symbol": last.get("symbol"),
            "updated_at": last.get("updated_at"),
            "message": str(last.get("message", ""))[:120],
        }
        print(safe)
except Exception as e:
    print(f"failed to read webhook_events.json: {e}")
PY
else
  echo "webhook_events.json: not found yet"
fi

echo
echo "===== Recent Log ====="
tail -80 "$APP_DIR/logs/kstockbot.log" 2>/dev/null || echo "no log yet"

echo "===== Doctor Done ====="

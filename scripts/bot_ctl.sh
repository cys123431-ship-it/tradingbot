#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${PID_FILE:-$HOME/emas.pid}"
LOG_FILE="${LOG_FILE:-$HOME/emas.log}"
PYTHON_BIN="${PYTHON_BIN:-$APP_DIR/venv/bin/python}"
BOT_ENTRY="${BOT_ENTRY:-$APP_DIR/emas.py}"
STATE_DIR="${STATE_DIR:-$APP_DIR/runtime}"
HEARTBEAT_FILE="${HEARTBEAT_FILE:-$STATE_DIR/bot_heartbeat.json}"
ACTION="${1:-}"
TRUNCATE_LOG="${TRUNCATE_LOG:-0}"

heartbeat_age_seconds() {
  if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    echo ""
    return 0
  fi

  local hb_mtime=""
  hb_mtime="$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || true)"
  if [[ -z "$hb_mtime" ]]; then
    hb_mtime="$(stat -f %m "$HEARTBEAT_FILE" 2>/dev/null || true)"
  fi
  if [[ -z "$hb_mtime" ]]; then
    echo ""
    return 0
  fi

  local now
  now="$(date +%s)"
  echo $(( now - hb_mtime ))
}

last_log_excerpt() {
  if [[ ! -f "$LOG_FILE" ]]; then
    echo ""
    return 0
  fi

  tail -n 3 "$LOG_FILE" 2>/dev/null \
    | tr '\r\n' '  ' \
    | sed 's/[[:space:]]\+/ /g' \
    | cut -c1-220
}

heartbeat_paused_state() {
  if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    echo ""
    return 0
  fi

  python3 - "$HEARTBEAT_FILE" <<'PY' 2>/dev/null || true
import json
import sys

path = sys.argv[1]
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    paused = data.get('paused', None)
    if isinstance(paused, bool):
        print('true' if paused else 'false')
    else:
        print('')
except Exception:
    print('')
PY
}

start_bot() {
  local launch_reason="${BOT_LAUNCH_REASON:-manual_start}"
  local last_heartbeat_age="${BOT_LAST_HEARTBEAT_AGE:-}"
  local last_pid="${BOT_LAST_PID:-}"
  local last_log_line="${BOT_LAST_LOG_LINE:-}"
  local prev_paused="${BOT_PREV_PAUSED:-}"

  if [[ -f "$PID_FILE" ]]; then
    local old_pid
    old_pid="$(cat "$PID_FILE" || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "bot already running: pid=$old_pid"
      return 0
    fi
    rm -f "$PID_FILE"
  fi

  if pgrep -f "$BOT_ENTRY" >/dev/null 2>&1; then
    echo "bot already running via process scan"
    return 0
  fi

  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "python binary not found: $PYTHON_BIN" >&2
    return 1
  fi

  mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")" "$STATE_DIR"
  if [[ "$TRUNCATE_LOG" == "1" ]]; then
    : > "$LOG_FILE"
  fi

  nohup env \
    BOT_LAUNCH_REASON="$launch_reason" \
    BOT_LAST_HEARTBEAT_AGE="$last_heartbeat_age" \
    BOT_LAST_PID="$last_pid" \
    BOT_LAST_LOG_LINE="$last_log_line" \
    BOT_PREV_PAUSED="$prev_paused" \
    BOT_HEARTBEAT_FILE="$HEARTBEAT_FILE" \
    BOT_START_TS="$(date -Is)" \
    "$PYTHON_BIN" "$BOT_ENTRY" >> "$LOG_FILE" 2>&1 < /dev/null &
  local new_pid=$!
  echo "$new_pid" > "$PID_FILE"
  sleep 3

  if ! kill -0 "$new_pid" 2>/dev/null; then
    echo "bot failed to start" >&2
    tail -n 50 "$LOG_FILE" || true
    return 1
  fi

  echo "bot started: pid=$new_pid"
}

stop_bot() {
  if [[ -f "$PID_FILE" ]]; then
    local old_pid
    old_pid="$(cat "$PID_FILE" || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      kill "$old_pid"
      sleep 2
    fi
    rm -f "$PID_FILE"
  fi

  if pgrep -f "$BOT_ENTRY" >/dev/null 2>&1; then
    pkill -f "$BOT_ENTRY" || true
    sleep 2
  fi

  echo "bot stopped"
}

status_bot() {
  if pgrep -f "$BOT_ENTRY" >/dev/null 2>&1; then
    pgrep -af "$BOT_ENTRY"
    return 0
  fi

  echo "bot not running"
  return 1
}

ensure_bot() {
  if pgrep -f "$BOT_ENTRY" >/dev/null 2>&1; then
    echo "bot healthy"
    return 0
  fi

  local last_pid=""
  if [[ -f "$PID_FILE" ]]; then
    last_pid="$(cat "$PID_FILE" || true)"
  fi

  BOT_LAUNCH_REASON="ensure_restart" \
  BOT_LAST_HEARTBEAT_AGE="$(heartbeat_age_seconds)" \
  BOT_LAST_PID="$last_pid" \
  BOT_LAST_LOG_LINE="$(last_log_excerpt)" \
  BOT_PREV_PAUSED="$(heartbeat_paused_state)" \
  start_bot
}

case "$ACTION" in
  start)
    start_bot
    ;;
  stop)
    stop_bot
    ;;
  restart)
    stop_bot
    BOT_LAUNCH_REASON="${BOT_LAUNCH_REASON:-manual_restart}" \
    BOT_PREV_PAUSED="$(heartbeat_paused_state)" \
    start_bot
    ;;
  status)
    status_bot
    ;;
  ensure)
    ensure_bot
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|ensure}" >&2
    exit 1
    ;;
esac

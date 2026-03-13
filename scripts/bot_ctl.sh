#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${PID_FILE:-$HOME/emas.pid}"
LOG_FILE="${LOG_FILE:-$HOME/emas.log}"
PYTHON_BIN="${PYTHON_BIN:-$APP_DIR/venv/bin/python}"
BOT_ENTRY="${BOT_ENTRY:-$APP_DIR/emas.py}"
ACTION="${1:-}"
TRUNCATE_LOG="${TRUNCATE_LOG:-0}"

start_bot() {
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

  mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"
  if [[ "$TRUNCATE_LOG" == "1" ]]; then
    : > "$LOG_FILE"
  fi

  nohup "$PYTHON_BIN" "$BOT_ENTRY" >> "$LOG_FILE" 2>&1 < /dev/null &
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

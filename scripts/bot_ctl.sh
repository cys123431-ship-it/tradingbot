#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${PID_FILE:-$HOME/emas.pid}"
LOG_FILE="${LOG_FILE:-$HOME/emas.log}"
PYTHON_BIN="${PYTHON_BIN:-$APP_DIR/venv/bin/python}"
BOT_ENTRY="${BOT_ENTRY:-$APP_DIR/scripts/launch_emas.py}"
STATE_DIR="${STATE_DIR:-$APP_DIR/runtime}"
HEARTBEAT_FILE="${HEARTBEAT_FILE:-$STATE_DIR/bot_heartbeat.json}"
HEARTBEAT_MAX_AGE_SEC="${HEARTBEAT_MAX_AGE_SEC:-180}"
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

heartbeat_health_status() {
  if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    echo "missing"
    return 1
  fi

  local age
  age="$(heartbeat_age_seconds)"
  if [[ -z "$age" ]]; then
    echo "unreadable"
    return 1
  fi

  if [[ "$age" -gt "$HEARTBEAT_MAX_AGE_SEC" ]]; then
    echo "stale:$age"
    return 1
  fi

  echo "ok:$age"
  return 0
}

bot_process_running() {
  pgrep -f "$BOT_ENTRY" >/dev/null 2>&1
}

bot_process_count() {
  pgrep -fc "$BOT_ENTRY" 2>/dev/null || true
}

legacy_bot_pids() {
  ps -eo pid=,args= | awk -v launcher="$BOT_ENTRY" '
    index($0, launcher) == 0 &&
    $0 ~ /[Pp]ython/ &&
    $0 ~ /(^|[[:space:]])([^[:space:]]*\/)?emas\.py([[:space:]]|$)/ {
      print $1
    }
  '
}

stop_legacy_bot_processes() {
  local pids
  pids="$(legacy_bot_pids)"
  if [[ -z "$pids" ]]; then
    return 0
  fi

  echo "stopping legacy direct emas.py process(es): $(echo "$pids" | tr '\n' ' ')"
  while read -r pid; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done <<< "$pids"
  sleep 2

  pids="$(legacy_bot_pids)"
  if [[ -n "$pids" ]]; then
    while read -r pid; do
      [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null || true
    done <<< "$pids"
    sleep 1
  fi
}

start_bot() {
  local launch_reason="${BOT_LAUNCH_REASON:-manual_start}"
  local last_heartbeat_age="${BOT_LAST_HEARTBEAT_AGE:-}"
  local last_pid="${BOT_LAST_PID:-}"
  local last_log_line="${BOT_LAST_LOG_LINE:-}"
  local prev_paused="${BOT_PREV_PAUSED:-}"

  stop_legacy_bot_processes

  if [[ -f "$PID_FILE" ]]; then
    local old_pid
    old_pid="$(cat "$PID_FILE" || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "bot already running: pid=$old_pid"
      return 0
    fi
    rm -f "$PID_FILE"
  fi

  if bot_process_running; then
    local running_count
    running_count="$(bot_process_count)"
    if [[ "$running_count" -eq 1 ]]; then
      echo "bot already running via process scan"
      return 0
    fi
    echo "multiple launcher processes detected: count=$running_count" >&2
    return 1
  fi

  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "python binary not found: $PYTHON_BIN" >&2
    return 1
  fi

  mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")" "$STATE_DIR"
  if [[ -f "$APP_DIR/runtime/prediction_live.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$APP_DIR/runtime/prediction_live.env"
    set +a
  fi
  if [[ "$TRUNCATE_LOG" == "1" ]]; then
    : > "$LOG_FILE"
  fi

  nohup env \
    PREDICTION_API_KEY="${PREDICTION_API_KEY:-}" \
    PREDICTION_JWT="${PREDICTION_JWT:-}" \
    PREDICTION_PRIVATE_KEY="${PREDICTION_PRIVATE_KEY:-}" \
    PREDICTION_PREDICT_ACCOUNT="${PREDICTION_PREDICT_ACCOUNT:-}" \
    PREDICTION_CHAIN_ID="${PREDICTION_CHAIN_ID:-56}" \
    PREDICTION_LIVE_TRADING_ENABLED="${PREDICTION_LIVE_TRADING_ENABLED:-0}" \
    PREDICTION_APPROVALS_CONFIRMED="${PREDICTION_APPROVALS_CONFIRMED:-0}" \
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

  if bot_process_running; then
    pkill -f "$BOT_ENTRY" || true
    sleep 2
  fi

  stop_legacy_bot_processes

  echo "bot stopped"
}

status_bot() {
  local legacy_pids
  legacy_pids="$(legacy_bot_pids)"
  if [[ -n "$legacy_pids" ]]; then
    echo "bot unhealthy: legacy direct emas.py process detected: $(echo "$legacy_pids" | tr '\n' ' ')"
    return 1
  fi

  if bot_process_running; then
    local running_count
    running_count="$(bot_process_count)"
    if [[ "$running_count" -ne 1 ]]; then
      echo "bot unhealthy: launcher process count=$running_count"
      pgrep -af "$BOT_ENTRY" || true
      return 1
    fi
    pgrep -af "$BOT_ENTRY"
    local hb_status
    if hb_status="$(heartbeat_health_status)"; then
      echo "heartbeat healthy: age=${hb_status#ok:}s max=${HEARTBEAT_MAX_AGE_SEC}s"
      return 0
    fi
    case "$hb_status" in
      stale:*)
        echo "bot unhealthy: heartbeat stale age=${hb_status#stale:}s max=${HEARTBEAT_MAX_AGE_SEC}s"
        ;;
      missing)
        echo "bot unhealthy: heartbeat missing: $HEARTBEAT_FILE"
        ;;
      *)
        echo "bot unhealthy: heartbeat unreadable: $HEARTBEAT_FILE"
        ;;
    esac
    return 1
  fi

  if [[ -f "$HEARTBEAT_FILE" ]]; then
    local hb_age
    hb_age="$(heartbeat_age_seconds)"
    echo "bot not running (last heartbeat age=${hb_age:-unknown}s)"
  else
    echo "bot not running"
  fi
  return 1
}

ensure_bot() {
  stop_legacy_bot_processes

  if bot_process_running; then
    local hb_status
    if hb_status="$(heartbeat_health_status)"; then
      echo "bot healthy: heartbeat age=${hb_status#ok:}s"
      return 0
    fi
    echo "bot unhealthy: heartbeat $hb_status; restarting"
    BOT_LAST_HEARTBEAT_AGE="$(heartbeat_age_seconds)" \
    BOT_LAST_PID="$(cat "$PID_FILE" 2>/dev/null || true)" \
    BOT_LAST_LOG_LINE="$(last_log_excerpt)" \
    BOT_PREV_PAUSED="$(heartbeat_paused_state)" \
    stop_bot
    BOT_LAUNCH_REASON="ensure_restart_stale_heartbeat" \
    start_bot
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

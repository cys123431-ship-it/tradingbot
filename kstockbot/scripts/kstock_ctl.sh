#!/bin/bash

# Configuration
APP_DIR="/home/azureuser/kstockbot"
PARENT_DIR="/home/azureuser"
PID_FILE="$APP_DIR/runtime/kstockbot.pid"
LOG_FILE="$APP_DIR/logs/kstockbot.log"
VENV_DIR="$APP_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

start() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p $PID > /dev/null; then
            echo "KStockBot is already running (PID $PID)."
            return
        fi
    fi

    echo "Starting KStockBot..."
    mkdir -p "$APP_DIR/runtime" "$APP_DIR/logs"
    cd "$PARENT_DIR" || exit 1
    
    if [ ! -x "$PYTHON_BIN" ]; then
        PYTHON_BIN="/usr/bin/python3"
    fi
    
    if [ -f "$APP_DIR/.env" ]; then
        set -a
        source "$APP_DIR/.env"
        set +a
    fi
    
    KSTOCK_HOST="${KSTOCK_HOST:-0.0.0.0}"
    KSTOCK_PORT="${KSTOCK_PORT:-8090}"
    echo "Binding KStockBot on ${KSTOCK_HOST}:${KSTOCK_PORT}"
    
    nohup "$PYTHON_BIN" -m uvicorn kstockbot.app.main:app --host "$KSTOCK_HOST" --port "$KSTOCK_PORT" >> "$LOG_FILE" 2>&1 &
    PID=$!
    echo $PID > "$PID_FILE"
    echo "Started (PID $PID)."
}

stop() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p $PID > /dev/null; then
            echo "Stopping KStockBot (PID $PID)..."
            kill $PID
            sleep 2
            if ps -p $PID > /dev/null; then
                echo "Force killing (PID $PID)..."
                kill -9 $PID
            fi
            echo "Stopped."
        else
            echo "KStockBot is not running."
        fi
        rm -f "$PID_FILE"
    else
        echo "PID file not found. Is KStockBot running?"
    fi
}

status() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p $PID > /dev/null; then
            echo "KStockBot is running (PID $PID)."
        else
            echo "KStockBot is not running (stale PID file found)."
        fi
    else
        echo "KStockBot is not running."
    fi
}

logs() {
    tail -f "$LOG_FILE"
}

health() {
    if [ -f "$APP_DIR/.env" ]; then
        set -a
        source "$APP_DIR/.env"
        set +a
    fi

    KSTOCK_PORT="${KSTOCK_PORT:-8090}"

    echo "===== /health ====="
    curl -fsS "http://127.0.0.1:${KSTOCK_PORT}/health" || echo "health failed"
    echo
    echo "===== /status ====="
    curl -fsS "http://127.0.0.1:${KSTOCK_PORT}/status" || echo "status failed"
    echo
}

case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        sleep 1
        start
        ;;
    status)
        status
        ;;
    health)
        health
        ;;
    logs)
        logs
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|health|logs}"
        exit 1
        ;;
esac

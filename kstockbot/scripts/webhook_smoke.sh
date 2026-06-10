#!/bin/bash
set -e

APP_DIR="${APP_DIR:-/home/azureuser/kstockbot}"

if [ -f "$APP_DIR/.env" ]; then
  set -a
  source "$APP_DIR/.env"
  set +a
else
  echo "ERROR: .env not found at $APP_DIR/.env"
  exit 1
fi

KSTOCK_PORT="${KSTOCK_PORT:-8090}"
KSTOCK_MODE="${KSTOCK_MODE:-analysis}"

if [ -z "${KSTOCK_WEBHOOK_SECRET:-}" ] || [ "${KSTOCK_WEBHOOK_SECRET:-}" = "change-me" ]; then
  echo "ERROR: KSTOCK_WEBHOOK_SECRET is not configured."
  exit 1
fi

echo "===== KStockBot Webhook Smoke Test ====="
echo "Mode: $KSTOCK_MODE"
echo "Port: $KSTOCK_PORT"
echo "Secret: SET"
echo

if [ "$KSTOCK_MODE" != "analysis" ]; then
  echo "WARNING: KSTOCK_MODE is not analysis."
  echo "This smoke test sends a BUY signal. In paper mode it may create a paper/mock order flow."
  echo "Press Ctrl+C within 5 seconds to cancel."
  sleep 5
fi

TMP_JSON=$(mktemp)
trap 'rm -f "$TMP_JSON"' EXIT

cat <<EOF > "$TMP_JSON"
{
  "secret": "${KSTOCK_WEBHOOK_SECRET}",
  "source": "smoke-test",
  "strategy": "webhook_smoke",
  "action": "BUY",
  "symbol": "005930",
  "price": 80000.75,
  "time": "$(date -Iseconds)",
  "timeframe": "1D",
  "confidence": 0.5,
  "reason": "local_smoke_test"
}
EOF

curl -sS -X POST "http://127.0.0.1:${KSTOCK_PORT}/webhook/tradingview" \
  -H "Content-Type: application/json" \
  --data-binary "@$TMP_JSON"

echo
echo "===== Done ====="

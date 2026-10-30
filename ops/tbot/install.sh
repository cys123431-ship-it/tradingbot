#!/usr/bin/env bash
# Install tbot binaries + systemd units shipped by the deploy workflow, then
# start or stop the services. Usage: install.sh <bundle dir> <enabled 0|1>
set -euo pipefail

BUNDLE="$1"
ENABLED="$2"
BIN_DIR=/home/azureuser/tbot/bin
UNITS=(tbot-engine tbot-telegram)

mkdir -p "$BIN_DIR"
for name in tbot-engine tbot-telegram; do
  install -m 0755 "$BUNDLE/$name" "$BIN_DIR/$name.new"
  mv -f "$BIN_DIR/$name.new" "$BIN_DIR/$name"
done
for unit in "${UNITS[@]}"; do
  sudo -n install -m 0644 "$BUNDLE/$unit.service" "/etc/systemd/system/$unit.service"
done
sudo -n systemctl daemon-reload

if [ "$ENABLED" != "1" ]; then
  echo "tbot: ops/TBOT_ENABLED absent -> services stopped"
  for unit in "${UNITS[@]}"; do
    sudo -n systemctl disable --now "$unit" 2>/dev/null || true
  done
  exit 0
fi

# One Telegram poller and one trader only: the Python bot must be off.
if pgrep -f "scripts/launch_emas.py" >/dev/null 2>&1; then
  echo "tbot: the Python bot is still running; refusing to start (add ops/BOT_DISABLED first)"
  exit 1
fi

for unit in "${UNITS[@]}"; do
  sudo -n systemctl enable "$unit" >/dev/null
  sudo -n systemctl restart "$unit"
done
sleep 10
for unit in "${UNITS[@]}"; do
  if ! systemctl is-active --quiet "$unit"; then
    echo "tbot: $unit failed to start"
    tail -n 40 "/home/azureuser/$unit.log" 2>/dev/null || true
    exit 1
  fi
done
echo "tbot: services active"
tail -n 5 /home/azureuser/tbot-engine.log 2>/dev/null || true

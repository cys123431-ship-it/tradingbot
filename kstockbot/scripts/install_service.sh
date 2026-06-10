#!/bin/bash

SERVICE_FILE="/etc/systemd/system/kstockbot.service"
APP_DIR="/home/azureuser/kstockbot"

echo "Checking environment..."
if [ ! -d "$APP_DIR/.venv" ]; then
    echo "Warning: .venv not found in $APP_DIR. Service relies on .venv/bin/python."
    echo "Please run: python3 -m venv $APP_DIR/.venv && source $APP_DIR/.venv/bin/activate && pip install -r $APP_DIR/requirements.txt"
fi

echo "Creating systemd service file..."

sudo tee "$SERVICE_FILE" >/dev/null <<'EOL'
[Unit]
Description=KStockBot Auto Trading Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=azureuser
WorkingDirectory=/home/azureuser
EnvironmentFile=-/home/azureuser/kstockbot/.env
ExecStart=/bin/bash -lc 'exec /home/azureuser/kstockbot/.venv/bin/python -m uvicorn kstockbot.app.main:app --host "${KSTOCK_HOST:-0.0.0.0}" --port "${KSTOCK_PORT:-8090}"'
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOL

sudo chmod 644 "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable kstockbot.service

echo "kstockbot.service installed and enabled."
echo "Use 'sudo systemctl start kstockbot' to start."

#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/www/server/panel/data/compose/heysureai2}"
UPDATE_SCRIPT="${UPDATE_SCRIPT:-$PROJECT_DIR/update-heysure.sh}"
WEBHOOK_HOST="${WEBHOOK_HOST:-0.0.0.0}"
WEBHOOK_PORT="${WEBHOOK_PORT:-8765}"
WEBHOOK_TOKEN="${WEBHOOK_TOKEN:-}"
SERVICE_NAME="heysure-update-webhook"
ENV_FILE="/etc/${SERVICE_NAME}.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root."
  exit 1
fi

if [[ -z "$WEBHOOK_TOKEN" ]]; then
  echo "WEBHOOK_TOKEN is required."
  echo "Example: WEBHOOK_TOKEN='a-long-random-token' bash $0"
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/server/other/scripts/deploy_webhook.py" ]]; then
  echo "Webhook bridge not found under PROJECT_DIR: $PROJECT_DIR"
  exit 1
fi

if [[ ! -f "$UPDATE_SCRIPT" ]]; then
  echo "Update script not found: $UPDATE_SCRIPT"
  exit 1
fi

chmod +x "$UPDATE_SCRIPT"

cat > "$ENV_FILE" <<EOF
WEBHOOK_TOKEN=$WEBHOOK_TOKEN
EOF
chmod 600 "$ENV_FILE"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=HeySure deployment update webhook
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $PROJECT_DIR/server/other/scripts/deploy_webhook.py --host $WEBHOOK_HOST --port $WEBHOOK_PORT --token \${WEBHOOK_TOKEN} --repo $PROJECT_DIR --script $UPDATE_SCRIPT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME"

echo
echo "Installed $SERVICE_NAME."
echo "Logs: journalctl -u $SERVICE_NAME -f"

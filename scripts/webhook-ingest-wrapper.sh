#!/bin/bash
# webhook-ingest-wrapper.sh — systemd wrapper for Webhook Ingest (FastAPI :8000)
# Runs 24/7 to durably receive TradingView webhooks into PostgreSQL.
set -e

HOME_DIR="${HOME_DIR:-/home/tradingapp}"
APP_DIR="${HOME_DIR}/app/backend"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"

cd "$APP_DIR"

echo "Starting Webhook Ingest (:8000)..." >&2
exec "$PYTHON_BIN" -m uvicorn app.webhook_ingest:app --host 127.0.0.1 --port 8000

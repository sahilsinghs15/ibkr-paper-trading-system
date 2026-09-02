#!/bin/bash
# webhook-ingest-wrapper.sh — systemd wrapper for Webhook Ingest (FastAPI :8000)
# Manages execution and off-market clean exit behavior.
set -e

HOME_DIR="${HOME_DIR:-/home/tradingapp}"
APP_DIR="${HOME_DIR}/app/backend"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
SESSION_GUARD="${HOME_DIR}/app/scripts/session_guard.py"

cd "$APP_DIR"

echo "Starting Webhook Ingest (:8000)..." >&2
set +e
"$PYTHON_BIN" -m uvicorn app.webhook_ingest:app --host 127.0.0.1 --port 8000
EXIT_CODE=$?
set -e

echo "Webhook Ingest process exited with code $EXIT_CODE" >&2

# Check market session window
if [ -f "$SESSION_GUARD" ] && "$PYTHON_BIN" "$SESSION_GUARD" >/dev/null 2>&1; then
  echo "Process exited during active trading session (09:30-16:00 ET). Preserving failure code $EXIT_CODE for systemd restart." >&2
  exit $EXIT_CODE
else
  echo "Process exited outside market trading hours (market closed). Exiting cleanly (code 0) to prevent unwanted auto-restart." >&2
  exit 0
fi

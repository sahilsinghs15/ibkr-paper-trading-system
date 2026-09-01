#!/bin/bash
# backend-ready-trigger.sh — wait for Backend health, then trigger Demo restart once
# Called as ExecStartPost of trading-backend.service (preserves Backend → Demo one-way)
set -e

TRIGGER="/home/tradingapp/storage/state/restart_demo.trigger"
HEALTH_URL="http://127.0.0.1:8001/health"
TIMEOUT=60
INTERVAL=1

echo "Waiting for Backend health $HEALTH_URL" >&2
START=$(date +%s)
while true; do
  NOW=$(date +%s)
  if [ $((NOW - START)) -ge $TIMEOUT ]; then
    echo "Backend not healthy after $TIMEOUT s, not triggering Demo" >&2
    exit 0
  fi
  if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
    echo "Backend healthy, touching Demo trigger $TRIGGER" >&2
    mkdir -p "$(dirname "$TRIGGER")"
    touch "$TRIGGER"
    echo "Touched $TRIGGER" >&2
    exit 0
  fi
  sleep $INTERVAL
done

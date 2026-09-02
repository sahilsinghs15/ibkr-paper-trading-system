#!/bin/bash
# ibgateway-wrapper.sh — systemd wrapper for Xvfb + IB Gateway (IBC)
# Preserves process_manager logic: Xvfb + Gateway as a pair, Gateway readiness (Login + TCP), then triggers Backend restart.
# This is the sole supervisor for Xvfb/Gateway (no Python process_manager for these in production).
set -e

HOME_DIR="${HOME_DIR:-/home/tradingapp}"
STORAGE_LOG_ROOT="${STORAGE_LOG_ROOT:-${HOME_DIR}/storage/logs}"
DISPLAY_NUM="99"
DISPLAY=":${DISPLAY_NUM}"
XVFB_SCREEN="1024x768x24"
IBC_SCRIPT="${HOME_DIR}/ibc/scripts/ibcstart.sh"
IBC_WAIT_ARG="1045"
TWS_PATH="${HOME_DIR}/Jts"
TWS_SETTINGS_PATH="${HOME_DIR}/Jts"
IBC_PATH="${HOME_DIR}/ibc"
IBC_INI="${HOME_DIR}/ibc/config.ini"
GATEWAY_HOST="127.0.0.1"
GATEWAY_PORT="4002"
GATEWAY_LOGIN_MARKER="Login has completed"
RESTART_BACKEND_TRIGGER="${HOME_DIR}/storage/state/restart_backend.trigger"

# Cleanup stale Xvfb locks (mirrors process_manager.clear_stale_xvfb_lock)
for p in "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"; do
  if [ -e "$p" ]; then
    echo "Removing stale Xvfb lock/socket: $p" >&2
    rm -f "$p" || true
  fi
done

# Kill orphaned Xvfb already holding display (mirrors kill_orphaned_xvfb)
if command -v pgrep >/dev/null 2>&1; then
  pids=$(pgrep -f "Xvfb ${DISPLAY} " || true)
  if [ -n "$pids" ]; then
    for pid in $pids; do
      echo "Terminating orphaned Xvfb PID $pid on ${DISPLAY}" >&2
      kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 1
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        echo "PID $pid still alive after SIGTERM, SIGKILL" >&2
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
  fi
fi

# Start Xvfb
echo "Starting Xvfb ${DISPLAY} -screen 0 ${XVFB_SCREEN}" >&2
Xvfb "${DISPLAY}" -screen 0 "${XVFB_SCREEN}" &
XVFB_PID=$!
echo "Xvfb started PID $XVFB_PID" >&2

# Ensure Xvfb is cleaned up on exit
cleanup() {
  echo "Cleaning up Xvfb PID $XVFB_PID" >&2
  kill -TERM "$XVFB_PID" 2>/dev/null || true
  sleep 1
  if kill -0 "$XVFB_PID" 2>/dev/null; then
    kill -KILL "$XVFB_PID" 2>/dev/null || true
  fi
  rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

sleep 2

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
  echo "Xvfb failed to start" >&2
  exit 1
fi

# Prepare Gateway log path (same as process_manager dated log)
LOG_DIR="${STORAGE_LOG_ROOT}/$(date +%Y-%m-%d)"
mkdir -p "$LOG_DIR"
GATEWAY_LOG="${LOG_DIR}/ib_gateway.log"
touch "$GATEWAY_LOG"
LOG_OFFSET=$(stat -c%s "$GATEWAY_LOG" 2>/dev/null || echo 0)
echo "Gateway log: $GATEWAY_LOG offset $LOG_OFFSET" >&2

# Start IB Gateway via IBC in background, log to gateway log
echo "Starting IB Gateway: $IBC_SCRIPT $IBC_WAIT_ARG --gateway" >&2
DISPLAY="$DISPLAY" "$IBC_SCRIPT" "$IBC_WAIT_ARG" --gateway --tws-path="$TWS_PATH" --tws-settings-path="$TWS_SETTINGS_PATH" --ibc-path="$IBC_PATH" --ibc-ini="$IBC_INI" >> "$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!
echo "IB Gateway started PID $GATEWAY_PID" >&2

# Wait for Gateway readiness: Login has completed + TCP 4002 open (mirrors wait_for_gateway_ready)
# This is required before Backend restart — preserves authentication dependency.
echo "Waiting for IB Gateway readiness (Login + TCP $GATEWAY_HOST:$GATEWAY_PORT)" >&2
TIMEOUT=180
START=$(date +%s)
LOGGED_IN=0
PORT_UP=0
while true; do
  NOW=$(date +%s)
  if [ $((NOW - START)) -ge $TIMEOUT ]; then
    echo "Gateway not ready after $TIMEOUT s (login=$LOGGED_IN port=$PORT_UP)" >&2
    break
  fi
  if [ $LOGGED_IN -eq 0 ]; then
    if tail -c +$((LOG_OFFSET+1)) "$GATEWAY_LOG" 2>/dev/null | grep -q "$GATEWAY_LOGIN_MARKER"; then
      echo "IBC reported Login has completed" >&2
      LOGGED_IN=1
    fi
  fi
  if nc -z "$GATEWAY_HOST" "$GATEWAY_PORT" 2>/dev/null; then
    if [ $PORT_UP -eq 0 ]; then
      echo "Gateway API listening on $GATEWAY_HOST:$GATEWAY_PORT" >&2
      PORT_UP=1
    fi
  else
    PORT_UP=0
  fi
  if [ $LOGGED_IN -eq 1 ] && [ $PORT_UP -eq 1 ]; then
    echo "Gateway is logged in and API port is ready, waiting 5s settle" >&2
    sleep 5
    if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
      echo "Gateway died during settle" >&2
      break
    fi
    if ! nc -z "$GATEWAY_HOST" "$GATEWAY_PORT" 2>/dev/null; then
      echo "Gateway port dropped during settle, continuing wait" >&2
      PORT_UP=0
      continue
    fi
    echo "Gateway ready, triggering Backend restart" >&2
    mkdir -p "$(dirname "$RESTART_BACKEND_TRIGGER")"
    touch "$RESTART_BACKEND_TRIGGER"
    echo "Touched $RESTART_BACKEND_TRIGGER for Backend restart" >&2
    break
  fi
  sleep 1
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "Gateway process exited before ready" >&2
    break
  fi
done

# Now wait on Gateway process (keeps service alive)
# If Gateway was already ready, we have triggered Backend; now just wait
wait "$GATEWAY_PID" || true
EXIT_CODE=$?
echo "IB Gateway exited with code $EXIT_CODE" >&2

PYTHON_BIN="${HOME_DIR}/app/backend/.venv/bin/python"
SESSION_GUARD="${HOME_DIR}/app/scripts/session_guard.py"

if [ -f "$SESSION_GUARD" ] && [ -x "$PYTHON_BIN" ] && "$PYTHON_BIN" "$SESSION_GUARD" >/dev/null 2>&1; then
  echo "Gateway process exited during active trading session (09:30-16:00 ET). Preserving failure code $EXIT_CODE for systemd restart." >&2
  exit $EXIT_CODE
else
  echo "Gateway process exited outside market trading hours (market closed). Exiting cleanly (code 0) to prevent unwanted auto-restart." >&2
  exit 0
fi


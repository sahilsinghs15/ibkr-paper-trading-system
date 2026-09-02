#!/bin/bash
# ngrok-control.sh — Start, stop, or check status of ngrok tmux session
set -e

SESSION_NAME="ngrok"
HOME_DIR="${HOME_DIR:-/home/tradingapp}"
NGROK_BIN="${HOME_DIR}/ngrok"
PORT="8000"

case "$1" in
  start)
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
      echo "ngrok tmux session '$SESSION_NAME' already exists"
    else
      echo "Starting ngrok in tmux session '$SESSION_NAME' on 127.0.0.1:$PORT"
      tmux new-session -d -s "$SESSION_NAME" "cd $HOME_DIR && $NGROK_BIN http 127.0.0.1:$PORT"
    fi
    ;;
  stop)
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
      echo "Stopping ngrok tmux session '$SESSION_NAME'"
      tmux kill-session -t "$SESSION_NAME" || true
    else
      echo "ngrok tmux session '$SESSION_NAME' is not running"
    fi
    ;;
  status)
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
      echo "ngrok tmux session '$SESSION_NAME' is ACTIVE"
      tmux list-sessions | grep "$SESSION_NAME"
    else
      echo "ngrok tmux session '$SESSION_NAME' is STOPPED"
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|status}"
    exit 1
    ;;
esac

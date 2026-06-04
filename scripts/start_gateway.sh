#!/usr/bin/env bash
# 启动统一 IM gateway 监听进程（channel_listener）。
# 后台运行；按 CHANNELS_ENABLED 启动各通道 listener + outbox drain。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs
LOG="logs/channel_listener.log"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

if command -v launchctl >/dev/null 2>&1 && [ "$(uname -s)" = "Darwin" ]; then
  LABEL="com.user.stockchannelgateway"
  if [ "${RESTART_GATEWAY:-0}" != "1" ] && launchctl list | awk '{print $3}' | grep -Fxq "$LABEL"; then
    echo "[+] IM gateway already running via launchctl label=$LABEL log=$LOG"
    exit 0
  fi
  launchctl remove "$LABEL" 2>/dev/null || true
  sleep 2
  launchctl submit -l "$LABEL" -- /usr/bin/env \
    PROJECT_ROOT="$PROJECT_ROOT" PYTHON_BIN="$PYTHON_BIN" LOG_ABS="$PROJECT_ROOT/$LOG" \
    /bin/bash -lc 'cd "$PROJECT_ROOT" && exec "$PYTHON_BIN" -m stock_codex.apps.channel_listener >> "$LOG_ABS" 2>&1'
  sleep 2
  if launchctl list | awk '{print $3}' | grep -Fxq "$LABEL"; then
    echo "[+] IM gateway started via launchctl label=$LABEL log=$LOG"
    exit 0
  fi
  echo "[-] launchctl failed to start IM gateway; recent log:" >&2
  tail -n 40 "$LOG" >&2 || true
  exit 1
fi

nohup "$PYTHON_BIN" -m stock_codex.apps.channel_listener >>"$LOG" 2>&1 &
PID=$!
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  echo "[-] IM gateway failed to stay running; recent log:" >&2
  tail -n 40 "$LOG" >&2 || true
  exit 1
fi
echo "[+] IM gateway started pid=$PID log=$LOG"

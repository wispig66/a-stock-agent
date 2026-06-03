#!/usr/bin/env bash
# 启动统一 IM gateway 监听进程（channel_listener）。
# 后台运行；按 CHANNELS_ENABLED 启动各通道 listener + outbox drain。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs
LOG="logs/channel_listener.log"
nohup uv run --no-sync python -m stock_codex.apps.channel_listener >>"$LOG" 2>&1 &
echo "[+] IM gateway started pid=$! log=$LOG"

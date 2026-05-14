#!/usr/bin/env bash
# 停止 start_tg_listener.sh 拉起的 daemon。
set -e
cd "$(dirname "$0")/.."

PIDFILE="data/tg_listener.pid"
if [ ! -f "$PIDFILE" ]; then
  echo "未找到 PID 文件，daemon 可能未通过 start_tg_listener.sh 启动" >&2
  exit 1
fi

PID="$(cat "$PIDFILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "已停 PID=$PID"
else
  echo "PID=$PID 已不存在"
fi
rm -f "$PIDFILE"

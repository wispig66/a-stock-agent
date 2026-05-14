#!/usr/bin/env bash
# TG 单股查询守护进程启动器。由 launchd KeepAlive 维护，崩溃自动拉起。

set -e
cd "$(dirname "$0")/.."

LOGDIR="logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/tg_listener_$(date +%Y%m%d).log"

for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
  if [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done
[ -n "$UV_BIN" ] || { echo "未找到 uv 可执行" >&2; exit 1; }

echo "=== tg_listener start $(date) ===" >> "$LOG"
export PYTHONUNBUFFERED=1
# --no-sync 避免与其它 uv-run 常驻进程（anomaly_loop / watch_loop）抢 uv cache lock；
# 依赖 .venv 已通过 uv sync 准备好（首次安装由 scripts/setup.sh 处理）
exec "$UV_BIN" run --no-sync scripts/tg_listener.py >> "$LOG" 2>&1

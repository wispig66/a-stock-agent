#!/usr/bin/env bash
# 在当前 shell 启动 tg_listener daemon（nohup 后台）。
# 用法：从仓库根目录跑 `bash scripts/start_tg_listener.sh`。
# 与 launchd plist 二选一：项目在 ~/Desktop 下时 launchd 因 TCC/FDA 限制无法启动
# uv（getcwd 阻塞），改用此脚本由用户交互式 shell 拉起即可绕过。
# 停止：`bash scripts/stop_tg_listener.sh` 或 kill $(cat data/tg_listener.pid)

set -e
cd "$(dirname "$0")/.."

LOGDIR="logs"
DATADIR="data"
mkdir -p "$LOGDIR" "$DATADIR"
LOG="$LOGDIR/tg_listener_$(date +%Y%m%d).log"
PIDFILE="$DATADIR/tg_listener.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "tg_listener 已在跑 (PID $(cat "$PIDFILE"))" >&2
  exit 1
fi

for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$HOME/anaconda3/bin/uv"; do
  if [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done
[ -n "${UV_BIN:-}" ] || { echo "未找到 uv" >&2; exit 1; }

echo "=== tg_listener start $(date) ===" >> "$LOG"
export PYTHONUNBUFFERED=1
nohup "$UV_BIN" run --no-sync scripts/tg_listener.py >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "tg_listener 启动 PID=$!，日志 → $LOG"

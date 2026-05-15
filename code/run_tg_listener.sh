#!/usr/bin/env bash
# TG 单股查询守护进程启动器。由 launchd KeepAlive 维护，崩溃自动拉起。

set -e
cd "$(dirname "$0")/.."

LOGDIR="logs"
mkdir -p "$LOGDIR"
# 固定文件名（不带日期），由外部 rotate 切日；常驻进程跨午夜也能正确 append
LOG="$LOGDIR/tg_listener.log"

for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
  if [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done
[ -n "$UV_BIN" ] || { echo "未找到 uv 可执行" >&2; exit 1; }

# 启动前 rotate：如果当前 LOG 已存在且 mtime 不是今天，归档为 tg_listener_YYYYMMDD.log
if [ -f "$LOG" ]; then
  LOG_MTIME=$(stat -f %Sm -t %Y%m%d "$LOG" 2>/dev/null || date +%Y%m%d)
  TODAY_DATE=$(date +%Y%m%d)
  if [ "$LOG_MTIME" != "$TODAY_DATE" ]; then
    mv "$LOG" "$LOGDIR/tg_listener_${LOG_MTIME}.log"
  fi
fi

echo "=== tg_listener start $(date) ===" >> "$LOG"
export PYTHONUNBUFFERED=1
# --no-sync 避免与其它 uv-run 常驻进程（anomaly_loop / watch_loop）抢 uv cache lock；
# 依赖 .venv 已通过 uv sync 准备好（首次安装由 scripts/setup.sh 处理）
exec "$UV_BIN" run --no-sync scripts/tg_listener.py >> "$LOG" 2>&1

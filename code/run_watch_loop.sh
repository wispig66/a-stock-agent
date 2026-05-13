#!/usr/bin/env bash
# 盘中阈值监控启动器。建议在 09:30 之前手动启动（或加到 launchd/cron 09:25 触发）。
# 自动结束于 15:00。

set -e
cd "$(dirname "$0")/.."

LOGDIR="logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/watch_loop_$(date +%Y%m%d).log"

echo "=== watch_loop start $(date) ===" >> "$LOG"
exec ~/miniconda3/envs/stock/bin/python .claude/skills/stock-intraday/scripts/watch_loop.py "$@" >> "$LOG" 2>&1

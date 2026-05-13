#!/usr/bin/env bash
# 全市场异动捕捉启动器。建议在 09:30 之前手动启动（与 run_watch_loop.sh 并行）。
# 自动结束于 15:00。

set -e
cd "$(dirname "$0")/.."

LOGDIR="logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/anomaly_loop_$(date +%Y%m%d).log"

echo "=== anomaly_loop start $(date) ===" >> "$LOG"
exec ~/miniconda3/envs/stock/bin/python .claude/skills/stock-anomaly/scripts/anomaly_loop.py "$@" >> "$LOG" 2>&1

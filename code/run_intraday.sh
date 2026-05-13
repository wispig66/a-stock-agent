#!/bin/bash
# 盘中 skill 调用脚本。launchd 在 09:30 / 09:45 / 11:30 / 14:30 触发。
# SKILL.md 内部按当前 date '+%H:%M' 自路由到对应分支。

set -e
cd /Users/wispig/Desktop/stock

LOG_DIR=logs
mkdir -p $LOG_DIR
TODAY=$(date +%Y-%m-%d)
NOW=$(date '+%H:%M')
LOGFILE="$LOG_DIR/intraday_${TODAY}.log"

for candidate in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" "/usr/local/bin/claude" "/opt/homebrew/bin/claude"; do
  if [ -x "$candidate" ]; then
    CLAUDE_BIN="$candidate"
    break
  fi
done
[ -n "$CLAUDE_BIN" ] || { echo "未找到 claude 可执行"; exit 1; }

{
  echo "=========================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 stock-intraday skill · 时点 $NOW"
  echo "=========================================="

  "$CLAUDE_BIN" -p "使用 stock-intraday skill 跑当前时点（$NOW）的盘中流程并推送 Telegram。完成后只返回简要总结。" \
    --permission-mode bypassPermissions \
    --output-format text \
    < /dev/null

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成"
} >> "$LOGFILE" 2>&1

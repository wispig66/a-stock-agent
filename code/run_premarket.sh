#!/bin/bash
# 盘前 skill 调用脚本。launchd / cron / 手动均可调。
# 工作日 08:30 触发；周末和节假日跑也无害（脚本内部回退到上一个完整交易日）。

set -e
cd "$(dirname "$0")/.."

LOG_DIR=logs
mkdir -p $LOG_DIR
TODAY=$(date +%Y-%m-%d)
LOGFILE="$LOG_DIR/premarket_${TODAY}.log"

# claude CLI 实际路径（launchd 没有 PATH，必须绝对）
for candidate in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" "/usr/local/bin/claude" "/opt/homebrew/bin/claude"; do
  if [ -x "$candidate" ]; then
    CLAUDE_BIN="$candidate"
    break
  fi
done
[ -n "$CLAUDE_BIN" ] || { echo "未找到 claude 可执行"; exit 1; }

{
  echo "=========================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 stock-premarket skill"
  echo "=========================================="

  "$CLAUDE_BIN" -p "使用 stock-premarket skill 生成今日 A 股盘前观察池并推送到 Telegram。完成后只返回简要总结。" \
    --permission-mode bypassPermissions \
    --output-format text \
    < /dev/null

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成"
} >> "$LOGFILE" 2>&1

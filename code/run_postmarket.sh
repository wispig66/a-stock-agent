#!/bin/bash
# 盘后 skill 调用脚本。工作日 15:30 触发；周末跑也无害（脚本内部判断当日是否有效交易日）。

set -e
cd "$(dirname "$0")/.."

LOG_DIR=logs
mkdir -p $LOG_DIR
TODAY=$(date +%Y-%m-%d)
LOGFILE="$LOG_DIR/postmarket_${TODAY}.log"

# claude CLI 实际路径
for candidate in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" "/usr/local/bin/claude" "/opt/homebrew/bin/claude"; do
  if [ -x "$candidate" ]; then
    CLAUDE_BIN="$candidate"
    break
  fi
done
[ -n "$CLAUDE_BIN" ] || { echo "未找到 claude 可执行"; exit 1; }

{
  echo "=========================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 stock-postmarket skill"
  echo "=========================================="

  "$CLAUDE_BIN" -p "使用 stock-postmarket skill 复盘今日 A 股盘前观察池，落库今日 sentiment 与同花顺题材数据，并推送明日预案到 Telegram。完成后只返回简要总结。" \
    --permission-mode bypassPermissions \
    --output-format text \
    < /dev/null

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 刷新 stock_basic（给 TG 单股查询用）"
  for c in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
    [ -x "$c" ] && { "$c" run --no-sync scripts/refresh_stock_basic.py \
      || echo "stock_basic 刷新失败（不阻断 postmarket 主流程）"; break; }
  done

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成"
} >> "$LOGFILE" 2>&1

#!/bin/bash
# Legacy manual wrapper for stock-intraday. Default scheduling is Codex automation.
# SKILL.md 内部按当前 date '+%H:%M' 自路由到对应分支。

set -e
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/anaconda3/bin:$HOME/.local/bin:$PATH"

export CARD_VALIDATOR_MODE=warn

LOG_DIR=logs
mkdir -p "$LOG_DIR"
TODAY=$(date +%Y-%m-%d)
NOW=$(date '+%H:%M')
LOGFILE="$LOG_DIR/intraday_${TODAY}.log"

for candidate in "$HOME/.nvm/versions/node/v24.15.0/bin/codex" "$HOME/.local/bin/codex" "/opt/homebrew/bin/codex" "/usr/local/bin/codex"; do
  if [ -x "$candidate" ]; then
    CODEX_BIN="$candidate"
    break
  fi
done
[ -n "$CODEX_BIN" ] || { echo "未找到 codex 可执行"; exit 1; }

{
  echo "=========================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 stock-intraday skill · 时点 $NOW"
  echo "=========================================="

  "$CODEX_BIN" exec --dangerously-bypass-approvals-and-sandbox -C "$PWD" - <<PROMPT
Use the stock-intraday skill in this repository. Run the current-time intraday workflow for $NOW and push it to Telegram. Return only a concise operational summary.
PROMPT

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成"
} >> "$LOGFILE" 2>&1

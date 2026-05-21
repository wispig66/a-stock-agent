#!/bin/bash
# Legacy manual wrapper for stock-postmarket. Default scheduling is Codex automation.

set -e
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/anaconda3/bin:$HOME/.local/bin:$PATH"

export CARD_VALIDATOR_MODE=warn

LOG_DIR=logs
mkdir -p "$LOG_DIR"
TODAY=$(date +%Y-%m-%d)
LOGFILE="$LOG_DIR/postmarket_${TODAY}.log"

for candidate in "$HOME/.nvm/versions/node/v24.15.0/bin/codex" "$HOME/.local/bin/codex" "/opt/homebrew/bin/codex" "/usr/local/bin/codex"; do
  if [ -x "$candidate" ]; then
    CODEX_BIN="$candidate"
    break
  fi
done
[ -n "$CODEX_BIN" ] || { echo "未找到 codex 可执行"; exit 1; }

{
  echo "=========================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 stock-postmarket skill"
  echo "=========================================="

  "$CODEX_BIN" exec --dangerously-bypass-approvals-and-sandbox -C "$PWD" - <<'PROMPT'
Use the stock-postmarket skill in this repository. Review today's A-share premarket plan, persist today's sentiment and theme data, and push tomorrow's plan to Telegram. Return only a concise operational summary.
PROMPT

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 刷新 stock_basic（给 TG 单股查询用）"
  for c in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$HOME/anaconda3/bin/uv"; do
    [ -x "$c" ] && { "$c" run --no-sync scripts/refresh_stock_basic.py \
      || echo "stock_basic 刷新失败（不阻断 postmarket 主流程）"; break; }
  done

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成"
} >> "$LOGFILE" 2>&1

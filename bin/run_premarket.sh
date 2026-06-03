#!/bin/bash
# Legacy manual wrapper for stock-premarket. Default scheduling is Codex automation.

set -e
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/anaconda3/bin:$HOME/.local/bin:$PATH"
export CARD_VALIDATOR_MODE=warn

LOG_DIR=logs
mkdir -p "$LOG_DIR"
TODAY=$(date +%Y-%m-%d)
LOGFILE="$LOG_DIR/premarket_${TODAY}.log"

for candidate in "$HOME/.nvm/versions/node/v24.15.0/bin/codex" "$HOME/.local/bin/codex" "/opt/homebrew/bin/codex" "/usr/local/bin/codex"; do
  if [ -x "$candidate" ]; then
    CODEX_BIN="$candidate"
    break
  fi
done
[ -n "$CODEX_BIN" ] || { echo "未找到 codex 可执行"; exit 1; }

for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$HOME/anaconda3/bin/uv"; do
  if [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done
[ -n "$UV_BIN" ] || { echo "未找到 uv 可执行"; exit 1; }

{
  echo "=========================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 stock-premarket Codex fallback"
  echo "=========================================="

  ALREADY_PUSHED=$("$UV_BIN" run --no-sync python -c "
import sqlite3
try:
    c = sqlite3.connect('data/daily.db')
    n = c.execute(\"SELECT COUNT(*) FROM push_log WHERE source='stock-premarket' AND date(timestamp)=date('now','localtime')\").fetchone()[0]
    print(n)
except Exception:
    print(0)
" 2>/dev/null)
  if [ "$ALREADY_PUSHED" -gt 0 ] 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ 今日 premarket 已推送（push_log 命中 $ALREADY_PUSHED 条），跳过补跑"
    exit 0
  fi

  if ! curl -sSf --max-time 3 -o /dev/null https://open.feishu.cn \
     && ! curl -sSf --max-time 3 -o /dev/null https://push2.eastmoney.com; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ 网络不通（feishu/eastmoney 3s 超时），放弃本次运行（等下次补跑触发）"
    exit 99
  fi

  "$CODEX_BIN" exec --dangerously-bypass-approvals-and-sandbox -C "$PWD" - <<'PROMPT'
Use the stock-premarket skill in this repository. Generate today's A-share premarket plan and push it to the IM gateway. Return only a concise operational summary.
PROMPT

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成"
} >> "$LOGFILE" 2>&1

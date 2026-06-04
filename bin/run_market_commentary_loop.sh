#!/usr/bin/env bash
# 事件驱动盘面动态 worker。建议 09:25 由 launchd 触发；排空尾盘队列后自动结束于 15:15。

set -e
cd "$(dirname "$0")/.."

LOGDIR="logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/market_commentary_loop_$(date +%Y%m%d).log"

for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$HOME/anaconda3/bin/uv"; do
  if [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done
[ -n "$UV_BIN" ] || { echo "未找到 uv 可执行；先 brew install uv 或 curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }

echo "=== market_commentary_loop start $(date) ===" >> "$LOG"
exec "$UV_BIN" run python -m stock_codex.apps.market_commentary_loop "$@" >> "$LOG" 2>&1

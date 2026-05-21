#!/usr/bin/env bash
# 全市场异动捕捉启动器。建议在 09:30 之前手动启动（与 run_watch_loop.sh 并行）。
# 自动结束于 15:00。

set -e
cd "$(dirname "$0")/.."

# card_validator 模式（见 docs/card_validator_enforce_switch.md）
export CARD_VALIDATOR_MODE=warn

LOGDIR="logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/anomaly_loop_$(date +%Y%m%d).log"

# uv 可执行路径（launchd 无 PATH，必须绝对）
for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$HOME/anaconda3/bin/uv"; do
  if [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done
[ -n "$UV_BIN" ] || { echo "未找到 uv 可执行；先 brew install uv 或 curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }

echo "=== anomaly_loop start $(date) ===" >> "$LOG"
exec "$UV_BIN" run .agents/skills/stock-anomaly/scripts/anomaly_loop.py "$@" >> "$LOG" 2>&1

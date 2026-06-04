#!/usr/bin/env bash
# 盘中新主线浮现 daemon 启动器（Layer 1）。
# 建议 09:25 由 launchd 触发；自动结束于 15:00。
#
# 用法：
#   bash run_theme_loop.sh                         # 只推 T2 主线确认
#   bash run_theme_loop.sh --push-level=all        # 推 T1 + T2
#   bash run_theme_loop.sh --push-level=shadow     # 影子模式（只写日志不推 IM）

set -e
cd "$(dirname "$0")/.."

LOGDIR="logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/theme_loop_$(date +%Y%m%d).log"

# uv 可执行路径（launchd 无 PATH，必须绝对）
for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$HOME/anaconda3/bin/uv"; do
  if [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done
[ -n "$UV_BIN" ] || { echo "未找到 uv 可执行；先 brew install uv 或 curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }

echo "=== theme_emergence_loop start $(date) ===" >> "$LOG"
exec "$UV_BIN" run python -m stock_codex.apps.theme_emergence_loop "$@" >> "$LOG" 2>&1

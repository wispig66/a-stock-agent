#!/bin/bash
# 盘前 skill 调用脚本。launchd / cron / 手动均可调。
# 工作日 08:30 触发；周末和节假日跑也无害（脚本内部回退到上一个完整交易日）。

set -e
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/anaconda3/bin:$HOME/.local/bin:$PATH"

# card_validator 模式（见 docs/card_validator_enforce_switch.md）
export CARD_VALIDATOR_MODE=warn

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

for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$HOME/anaconda3/bin/uv"; do
  if [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done
[ -n "$UV_BIN" ] || { echo "未找到 uv 可执行"; exit 1; }

CLAUDE_TIMEOUT_SEC=${PREMARKET_CLAUDE_TIMEOUT_SEC:-900}

{
  echo "=========================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 stock-premarket skill"
  echo "=========================================="

  # 幂等检查：今日已成功推送过 → 跳过（支持 launchd 多次触发补跑）
  ALREADY_PUSHED=$("$UV_BIN" run --no-sync python -c "
import sqlite3, sys
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

  # 网络可用性检查：通勤无网时直接 exit 99，避免 SKILL hang 1h+ 把 LLM session 卡死
  # 探测 telegram + eastmoney 任一可达即视为有网
  if ! curl -sSf --max-time 3 -o /dev/null https://api.telegram.org \
     && ! curl -sSf --max-time 3 -o /dev/null https://push2.eastmoney.com; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ 网络不通（telegram/eastmoney 3s 超时），放弃本次运行（等下次补跑触发）"
    exit 99
  fi

  "$CLAUDE_BIN" -p "使用 stock-premarket skill 生成今日 A 股盘前观察池并推送到 Telegram。完成后只返回简要总结。" \
    --permission-mode bypassPermissions \
    --output-format text \
    < /dev/null &
  CLAUDE_PID=$!
  WAITED=0
  while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ "$WAITED" -ge "$CLAUDE_TIMEOUT_SEC" ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ claude 超时 ${CLAUDE_TIMEOUT_SEC}s，终止本次 premarket"
      kill "$CLAUDE_PID" 2>/dev/null || true
      sleep 2
      kill -9 "$CLAUDE_PID" 2>/dev/null || true
      wait "$CLAUDE_PID" 2>/dev/null || true
      exit 124
    fi
    sleep 5
    WAITED=$((WAITED + 5))
    if [ $((WAITED % 60)) -eq 0 ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] claude 仍在运行，已用 ${WAITED}s"
    fi
  done
  set +e
  wait "$CLAUDE_PID"
  CLAUDE_STATUS=$?
  set -e
  if [ "$CLAUDE_STATUS" -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ claude 退出码 $CLAUDE_STATUS"
    exit "$CLAUDE_STATUS"
  fi

  echo
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成"
} >> "$LOGFILE" 2>&1

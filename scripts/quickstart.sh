#!/usr/bin/env bash
# 新用户快速安装 + 启动脚本。
#
# 默认行为：
#   1. 检查/安装 uv，检查 sqlite3
#   2. 同步依赖
#   3. 初始化 SQLite 和运行通道迁移
#   4. 创建/补全 .env，写入飞书等 IM 通道配置
#   5. 设置脚本权限
#   6. 启动统一 IM gateway
#
# 常用：
#   bash scripts/quickstart.sh
#   bash scripts/quickstart.sh --no-start
#   bash scripts/quickstart.sh --install-schedule
#   bash scripts/quickstart.sh --with-feishu

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

START_GATEWAY=1
INSTALL_SCHEDULE=0
RUN_TESTS=0
CONFIGURE_FEISHU=0

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok() { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$1"; }
err() { printf "${RED}✗${NC} %s\n" "$1" >&2; }
step() { printf "\n${GREEN}▶${NC} %s\n" "$1"; }

usage() {
  cat <<'EOF'
用法：bash scripts/quickstart.sh [选项]

选项：
  --no-start           只安装和初始化，不启动 IM gateway
  --install-schedule   安装 Codex automations 和 launchd 长时任务
  --with-feishu        安装后进入飞书配置向导
  --test               安装后跑 pytest
  -h, --help           显示帮助

也可以通过环境变量跳过交互：
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-start)
      START_GATEWAY=0
      ;;
    --install-schedule)
      INSTALL_SCHEDULE=1
      ;;
    --with-feishu)
      CONFIGURE_FEISHU=1
      ;;
    --test)
      RUN_TESTS=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "未知参数：$1"
      usage
      exit 2
      ;;
  esac
  shift
done

env_get() {
  local key="$1"
  if [ ! -f .env ]; then
    return 0
  fi
  grep -E "^${key}=" .env | tail -1 | cut -d= -f2- | sed -E 's/^["'\'']?//; s/["'\'']?$//' || true
}

env_set() {
  local key="$1"
  local value="$2"
  python - "$key" "$value" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

key, value = sys.argv[1], sys.argv[2]
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
prefix = f"{key}="
updated = False
next_lines: list[str] = []
for line in lines:
    if line.startswith(prefix):
        if not updated:
            next_lines.append(f"{key}={value}")
            updated = True
        continue
    next_lines.append(line)
if not updated:
    next_lines.append(f"{key}={value}")
path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
PY
}

prompt_secret_if_empty() {
  local key="$1"
  local label="$2"
  local value="${!key:-}"
  if [ -z "$value" ]; then
    value="$(env_get "$key")"
  fi
  if [ -n "$value" ]; then
    env_set "$key" "$value"
    return 0
  fi
  if [ -t 0 ]; then
    printf "%s: " "$label"
    stty -echo
    IFS= read -r value
    stty echo
    printf "\n"
    if [ -n "$value" ]; then
      env_set "$key" "$value"
      return 0
    fi
  fi
  return 1
}

prompt_plain_if_empty() {
  local key="$1"
  local label="$2"
  local value="${!key:-}"
  if [ -z "$value" ]; then
    value="$(env_get "$key")"
  fi
  if [ -n "$value" ]; then
    env_set "$key" "$value"
    return 0
  fi
  if [ -t 0 ]; then
    printf "%s: " "$label"
    IFS= read -r value
    if [ -n "$value" ]; then
      env_set "$key" "$value"
      return 0
    fi
  fi
  return 1
}

step "[1/8] 检查基础工具"
if [ "$(uname -s)" != "Darwin" ]; then
  warn "当前不是 macOS。核心 Python 逻辑可运行，但 launchd 调度只支持 macOS。"
fi

if ! command -v uv >/dev/null 2>&1; then
  warn "未找到 uv，尝试使用官方安装脚本安装到当前用户目录"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  err "uv 安装失败。请手动安装后重试：brew install uv"
  exit 1
fi
ok "uv: $(uv --version)"

if ! command -v sqlite3 >/dev/null 2>&1; then
  err "未找到 sqlite3。macOS 通常自带；如缺失，请先运行：brew install sqlite"
  exit 1
fi
ok "sqlite3: $(sqlite3 --version | awk '{print $1}')"

step "[2/8] 安装 Python 依赖"
uv sync --group dev
ok "依赖同步完成"

step "[3/8] 初始化本地目录和数据库"
mkdir -p data logs
if [ ! -f data/daily.db ]; then
  sqlite3 data/daily.db < stock_codex/schema/init_db.sql
  ok "data/daily.db 已创建"
else
  ok "data/daily.db 已存在"
fi
uv run --no-sync python scripts/migrate_channels.py
ok "数据库迁移完成"

step "[4/8] 准备 .env"
if [ ! -f .env ]; then
  cp .env.example .env
  ok "已从 .env.example 创建 .env"
else
  ok ".env 已存在，将只补全关键项"
fi

[ -n "$(env_get CHANNEL_DEFAULT)" ] || env_set CHANNEL_DEFAULT feishu
[ -n "$(env_get CHANNELS_ENABLED)" ] || env_set CHANNELS_ENABLED feishu,weixin
[ -n "$(env_get CHANNELS_NOTIFY)" ] || env_set CHANNELS_NOTIFY feishu,weixin
ok ".env 已就绪（飞书凭证将在下一步用 configure_feishu.py 写入，未打印任何 secret）"

step "[5/8] 刷新交易日历"
if uv run --no-sync python -m stock_codex.tools.refresh_calendar; then
  ok "交易日历已刷新"
else
  warn "交易日历刷新失败，可能是网络或数据源问题；后续可重跑：uv run --no-sync python -m stock_codex.tools.refresh_calendar"
fi

step "[6/8] 设置脚本权限"
chmod +x bin/run_*.sh scripts/*.sh
ok "脚本权限已设置"

if [ "$CONFIGURE_FEISHU" -eq 1 ]; then
  step "[7/8] 配置飞书"
  uv run --no-sync python scripts/configure_feishu.py
else
  step "[7/8] 跳过飞书配置"
  echo "需要飞书时运行：uv run --no-sync python scripts/configure_feishu.py"
fi

if [ "$INSTALL_SCHEDULE" -eq 1 ]; then
  step "[8/8] 安装定时任务和长时 daemon"
  bash scripts/sync_codex_skills.sh
  bash scripts/install_codex_automations.sh
  bash scripts/install_runtime_services.sh
  ok "调度安装完成"
else
  step "[8/8] 启动或输出下一步"
  echo "暂不安装定时调度。需要自动盘前/盘中/盘后任务时运行："
  echo "  bash scripts/sync_codex_skills.sh"
  echo "  bash scripts/install_codex_automations.sh"
  echo "  bash scripts/install_runtime_services.sh"
fi

if [ "$RUN_TESTS" -eq 1 ]; then
  step "运行测试"
  uv run --no-sync pytest -q
fi

if [ "$START_GATEWAY" -eq 1 ]; then
  step "启动统一 IM gateway"
  bash scripts/start_gateway.sh
  ok "已启动。现在可以在飞书或微信发送 /help、股票代码或 /ask 问题。"
else
  ok "安装完成，未启动 gateway。启动命令：bash scripts/start_gateway.sh"
fi

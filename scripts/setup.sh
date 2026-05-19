#!/usr/bin/env bash
# 一键启动脚本 · 新机迁移 / 初次安装
#
# 用法：从仓库根目录跑 `bash scripts/setup.sh`
# 幂等：重复跑不会破坏现有 .env / 数据库
#
# 做的事：
#   1. 检查必备工具（uv / sqlite3）
#   2. uv sync --group dev（拉所有依赖含测试）
#   3. 初始化 data/daily.db（如不存在）
#   4. 拉 akshare 交易日历到 data/trade_calendar.csv
#   5. 检查 .env 是否填好
#   6. 输出调度安装指引

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok() { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$1"; }
err() { printf "${RED}✗${NC} %s\n" "$1" >&2; }
step() { printf "\n${GREEN}▶${NC} %s\n" "$1"; }

# ── Step 1: 必备工具 ────────────────────────────────────────
step "[1/6] 检查必备工具"

if ! command -v uv >/dev/null 2>&1; then
    err "未找到 uv。请先装："
    echo "    brew install uv          # macOS"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh   # 通用"
    exit 1
fi
ok "uv: $(uv --version)"

if ! command -v sqlite3 >/dev/null 2>&1; then
    err "未找到 sqlite3。macOS 自带，若缺失：brew install sqlite"
    exit 1
fi
ok "sqlite3: $(sqlite3 --version | awk '{print $1}')"

# ── Step 2: 依赖 ──────────────────────────────────────────────
step "[2/6] uv sync --group dev"
uv sync --group dev
ok "依赖同步完成"

# ── Step 3: SQLite ───────────────────────────────────────────
step "[3/6] 初始化 data/daily.db"
mkdir -p data logs
if [ -f data/daily.db ]; then
    ok "data/daily.db 已存在，跳过"
else
    sqlite3 data/daily.db < code/init_db.sql
    ok "data/daily.db 已创建"
fi

# ── Step 4: 交易日历 ──────────────────────────────────────────
step "[4/6] 拉交易日历到 data/trade_calendar.csv"
if [ -f data/trade_calendar.csv ]; then
    ok "data/trade_calendar.csv 已存在，跳过（如需刷新：uv run python code/refresh_calendar.py）"
else
    uv run python code/refresh_calendar.py
fi

# ── Step 5: .env ─────────────────────────────────────────────
step "[5/6] 检查 .env"
if [ ! -f .env ]; then
    cp .env.example .env
    warn ".env 已从 .env.example 创建，请编辑填入 TG_BOT_TOKEN / TG_CHAT_ID 后再继续"
    echo "    编辑：vim .env"
    echo "    填好后重跑此脚本。"
    exit 0
fi

# 简单校验（不打印 token 本身）
if grep -qE '^TG_BOT_TOKEN=\s*$' .env || grep -qE '^TG_CHAT_ID=\s*$' .env; then
    err ".env 中 TG_BOT_TOKEN 或 TG_CHAT_ID 为空，请先填入"
    exit 1
fi
ok ".env 已填值"

# ── Step 6: 权限 + 调度安装指引 ─────────────────────────────
step "[6/6] 给 shell 脚本加可执行权限"
chmod +x code/run_*.sh scripts/*.sh
ok "脚本可执行"

echo
ok "Setup 完成。基础环境已初始化。"
echo
echo "下一步按需安装调度："
echo "  bash scripts/sync_codex_skills.sh"
echo "  bash scripts/install_codex_automations.sh"
echo "  bash scripts/install_runtime_services.sh"
echo
echo "手动触发（不等定时）："
echo "  bash code/run_premarket.sh    # L1 盘前"
echo "  bash code/run_intraday.sh     # L2 盘中（按系统时间路由到对应时点）"
echo "  bash code/run_watch_loop.sh   # 中间时段轮询（前台跑）"
echo "  bash code/run_postmarket.sh   # L4 盘后"
echo
echo "跑测试："
echo "  uv run pytest tests/"

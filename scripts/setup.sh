#!/usr/bin/env bash
# 一键启动脚本 · 新机迁移 / 初次安装
#
# 用法：从仓库根目录跑 `bash scripts/setup.sh`
# 幂等：重复跑不会破坏现有 .env / 数据库 / plist
#
# 做的事：
#   1. 检查必备工具（uv / sqlite3）
#   2. uv sync --group dev（拉所有依赖含测试）
#   3. 初始化 data/daily.db（如不存在）
#   4. 拉 akshare 交易日历到 data/trade_calendar.csv
#   5. 检查 .env 是否填好
#   6. 渲染 launchd plist 模板（用当前路径替换 {{PROJECT_ROOT}}）→ ~/Library/LaunchAgents/
#   7. launchctl bootstrap 加载所有任务
#   8. 跑一次 notify test 验证 Telegram 连通

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
step "[1/8] 检查必备工具"

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

if ! command -v claude >/dev/null 2>&1; then
    warn "未检测到 Claude Code CLI（claude 命令）。skill 调用需要此 CLI。"
    warn "  装好后再跑 skill 触发，但本 setup 不阻断。"
else
    ok "claude: $(claude --version 2>/dev/null | head -1 || echo installed)"
fi

# ── Step 2: 依赖 ──────────────────────────────────────────────
step "[2/8] uv sync --group dev"
uv sync --group dev
ok "依赖同步完成"

# ── Step 3: SQLite ───────────────────────────────────────────
step "[3/8] 初始化 data/daily.db"
mkdir -p data logs
if [ -f data/daily.db ]; then
    ok "data/daily.db 已存在，跳过"
else
    sqlite3 data/daily.db < code/init_db.sql
    ok "data/daily.db 已创建"
fi

# ── Step 4: 交易日历 ──────────────────────────────────────────
step "[4/8] 拉交易日历到 data/trade_calendar.csv"
if [ -f data/trade_calendar.csv ]; then
    ok "data/trade_calendar.csv 已存在，跳过（如需刷新：uv run python code/refresh_calendar.py）"
else
    uv run python code/refresh_calendar.py
fi

# ── Step 5: .env ─────────────────────────────────────────────
step "[5/8] 检查 .env"
if [ ! -f .env ]; then
    cp .env.example .env
    warn ".env 已从 .env.example 创建，请编辑填入 TG_BOT_TOKEN / TG_CHAT_ID 后再继续"
    echo "    编辑：vim .env"
    echo "    填好后重跑此脚本完成剩余步骤。"
    exit 0
fi

# 简单校验（不打印 token 本身）
if grep -qE '^TG_BOT_TOKEN=\s*$' .env || grep -qE '^TG_CHAT_ID=\s*$' .env; then
    err ".env 中 TG_BOT_TOKEN 或 TG_CHAT_ID 为空，请先填入"
    exit 1
fi
ok ".env 已填值"

# ── Step 6: launchd plist 渲染 + 安装 ──────────────────────
step "[6/8] 安装 launchd 任务"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCHD_DIR"

for plist_template in launchd/com.user.stock*.plist; do
    [ -f "$plist_template" ] || continue
    plist_name=$(basename "$plist_template")
    target="$LAUNCHD_DIR/$plist_name"

    # 卸载已存在的（幂等）
    if launchctl print "gui/$(id -u)/${plist_name%.plist}" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)" "$target" 2>/dev/null || true
    fi

    # 渲染模板 → 替换 {{PROJECT_ROOT}}
    sed "s|{{PROJECT_ROOT}}|$PROJECT_ROOT|g" "$plist_template" > "$target"
    ok "渲染 $plist_name → $target"

    # 加载
    launchctl bootstrap "gui/$(id -u)" "$target"
    ok "已加载 $plist_name"
done

# ── Step 7: shell 脚本可执行权限 ─────────────────────────────
step "[7/8] 给 run_*.sh 加可执行权限"
chmod +x code/run_*.sh scripts/*.sh
ok "脚本可执行"

# ── Step 8: 验证 Telegram ──────────────────────────────────
step "[8/8] 测试 Telegram 推送"
if uv run python code/notify.py test 2>&1 | tail -5; then
    ok "Telegram 推送通了，检查你的 Telegram 应该收到一条 test 消息"
else
    err "Telegram 推送失败，检查 .env 里的 TG_BOT_TOKEN / TG_CHAT_ID"
    exit 1
fi

# ── Done ──────────────────────────────────────────────────────
echo
ok "Setup 完成。系统将在下个交易日的对应时点自动运行（08:30 / 09:25 / 09:30 / 09:45 / 11:30 / 14:30 / 15:35）。"
echo
echo "手动触发（不等定时）："
echo "  bash code/run_premarket.sh    # L1 盘前"
echo "  bash code/run_intraday.sh     # L2 盘中（按系统时间路由到对应时点）"
echo "  bash code/run_watch_loop.sh   # 中间时段轮询（前台跑）"
echo "  bash code/run_postmarket.sh   # L4 盘后"
echo
echo "重装 launchd 任务（修改 plist 后）："
echo "  bash scripts/install_launchd.sh"
echo
echo "跑测试："
echo "  uv run pytest tests/"

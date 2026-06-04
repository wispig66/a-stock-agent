# Automations Runbook

本文说明本机运行模型：短时 LLM jobs 通过可配置的 AI agent 调度，长时 watcher daemon 留在本机 launchd。当前机器就是交易 workflow 的 runtime。

## Agent 选择

系统支持多种 AI agent 执行定时 LLM 任务。通过 `config/jobs.yaml` 的 `agent` 字段或 `STOCK_AGENT` 环境变量选择：

| Agent | 调度方式 | CLI |
|-------|---------|-----|
| **codex**（默认） | 原生 Codex automation（TOML → `~/.codex/automations/`） | `codex exec` |
| **claude-code** | 原生 Local Scheduled Tasks（SKILL.md → `~/.claude/scheduled-tasks/`） | `claude -p` |
| **cline** | 原生 CLI cron | `cline` |
| **openclaw** | 原生 gateway cron | `openclaw cron` |
| **hermes** | 原生 cron | `hermes cron` |
| **opencode** | launchd 兜底 + `opencode run` | `opencode run` |
| **kimicode** | launchd 兜底 + `kimi --print` | `kimi --print` |

切换 agent：

```bash
# 方式 1: 修改 config/jobs.yaml 的 agent 字段
# 方式 2: 环境变量覆盖
export STOCK_AGENT=claude-code

# 安装（自动使用对应 agent 的原生调度）
bash scripts/install_automations.sh install

# 查看当前配置
bash scripts/install_automations.sh show

# 切换 agent 时先卸载旧的再安装新的
bash scripts/install_automations.sh install --replace --agent hermes

# 干跑检查
bash scripts/install_automations.sh install --agent claude-code --dry-run
```

## 部署模型

部署只保留本机路径：

- 本机安装 Python 依赖和 skills。
- 短时 LLM jobs 由选定 agent 的调度机制管理。
- 本机 `~/Library/LaunchAgents/` 是长时 daemon 的唯一 launchd 来源。

## 本机安装

```bash
cd /path/to/a-stock-agent

uv sync --group dev
cp .env.example .env
# 编辑 .env，填飞书和微信 iLink 等 IM 运行参数

mkdir -p data
sqlite3 data/daily.db < stock_codex/schema/init_db.sql
uv run --no-sync python scripts/migrate_channels.py
uv run python -m stock_codex.tools.refresh_calendar

bash scripts/sync_codex_skills.sh
bash scripts/install_automations.sh install      # 统一入口，按 config/jobs.yaml 选 agent
bash scripts/install_runtime_services.sh
bash scripts/start_gateway.sh
bash scripts/doctor_codex_runtime.sh
```

`scripts/install_automations.sh` 负责短时 LLM jobs；`scripts/install_runtime_services.sh` 负责长时 launchd daemon。旧入口 `scripts/install_codex_automations.sh` 仍可用（自动委托到新统一入口）。

## Active Jobs

这些 jobs 是短时 LLM workflow，统一定义在 `config/jobs.yaml`：

| Job ID | Schedule | Task |
|---|---:|---|
| `stock-premarket` | Mon-Fri 08:00 | Run `stock-premarket` and push through IM gateway |
| `stock-intraday-09-30` | Mon-Fri 09:30 | Run `stock-intraday` current-time branch |
| `stock-intraday-09-45` | Mon-Fri 09:45 | Run `stock-intraday` current-time branch |
| `stock-intraday-11-30` | Mon-Fri 11:30 | Run `stock-intraday` current-time branch |
| `stock-intraday-14-30` | Mon-Fri 14:30 | Run `stock-intraday` current-time branch |
| `stock-postmarket` | Mon-Fri 15:35 | Run `stock-postmarket` and refresh daily data |
| `stock-weekly-review` | Sun 21:00 | Run `stock-weekly` |

## What Still Uses launchd

长时 watcher daemon 继续使用 launchd（与 agent 选择无关）：

- `com.user.stockwatchloop`
- `com.user.stockanomalyloop`
- `com.user.stockthemeloop`
- `com.user.stockchannelgateway`

这些任务是盘中常驻进程或长时间轮询，不是短时 LLM jobs。`stockchannelgateway` 需要常驻，因为微信出站依赖 listener 持有的 iLink 连接和 outbox drain。

## Legacy

旧版短时 LLM launchd jobs 已迁移。确认新 jobs 已安装后，可以禁用旧 short-job launchd：

```bash
bash scripts/disable_legacy_llm_launchd.sh
```

## Verification

```bash
bash scripts/install_automations.sh verify
bash scripts/doctor_codex_runtime.sh
uv run pytest tests/test_jobs_yaml.py tests/test_jobs_loader.py tests/test_codex_automations.py -q
```

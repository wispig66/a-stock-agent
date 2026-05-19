# Codex Automations Runbook

本文面向开源用户，说明如何把短时 LLM jobs 放到 Codex automations，把长时 watcher daemon 留在 launchd。关键原则：Codex automations 必须安装在实际运行交易 workflow 的 runtime host 上。

## 部署模型

部署采用 pull-based 模型：

- runtime host 自己 `clone` / `pull` 仓库，自己安装 Python 依赖、Codex skills、Codex automations 和 launchd daemon。
- 开发机只通过 SSH 触发远程部署，不把当前工作区直接同步到 runtime host。
- 如果你在开发机的 Codex app 里创建 automation，它只属于开发机，不会自动出现在 runtime host。

## Runtime host 本地安装

在实际运行交易 workflow 的 runtime host 上执行：

```bash
git clone https://github.com/wispig66/a-stock-agent.git stock
cd stock

uv sync --group dev
cp .env.example .env
# 编辑 .env，填 Telegram bot token / chat_id 等运行参数

sqlite3 data/daily.db < code/init_db.sql
uv run python code/refresh_calendar.py

bash scripts/sync_codex_skills.sh
bash scripts/install_codex_automations.sh
bash scripts/install_runtime_services.sh
bash scripts/doctor_codex_runtime.sh
```

`scripts/install_codex_automations.sh` 只负责短时 LLM jobs；`scripts/install_runtime_services.sh` 负责长时 launchd daemon。

## 开发机 SSH 触发远程部署

开发机只保留远程连接配置，然后触发 runtime host 自己 pull/install：

```bash
cp deploy.remote.example.env deploy.remote.env
# 编辑 deploy.remote.env，填 SSH 目标、runtime host 仓库路径、分支等
bash scripts/deploy_remote_codex.sh
```

`deploy.remote.env` 只描述远程部署位置和分支，不包含 Telegram token/chat_id。runtime host 上的 `.env` 必须先存在且填好；首次远程部署如果只生成了 `.env` 模板，需要先 SSH 到 runtime host 填写 `.env`，再重跑部署或 doctor。

远程部署后，在 runtime host 上用 doctor 检查：

```bash
bash scripts/doctor_codex_runtime.sh
```

## Active Codex Jobs

这些 jobs 是短时 LLM workflow，由 Codex automations 调度：

| Job ID | Schedule | Task |
|---|---:|---|
| `stock-premarket` | Mon-Fri 08:30 | Run `stock-premarket` and push Telegram |
| `stock-intraday-09-30` | Mon-Fri 09:30 | Run `stock-intraday` current-time branch |
| `stock-intraday-09-45` | Mon-Fri 09:45 | Run `stock-intraday` current-time branch |
| `stock-intraday-11-30` | Mon-Fri 11:30 | Run `stock-intraday` current-time branch |
| `stock-intraday-14-30` | Mon-Fri 14:30 | Run `stock-intraday` current-time branch |
| `stock-postmarket` | Mon-Fri 15:35 | Run `stock-postmarket` and refresh daily data |
| `stock-weekly-review` | Sun 21:00 | Run `stock-weekly` |

Codex 在 runtime host 上保存 automation 文件的位置：

```bash
~/.codex/automations/
```

## What Still Uses launchd

长时 watcher daemon 继续使用 launchd：

- `com.user.stockwatchloop`
- `com.user.stockanomalyloop`
- `com.user.stockthemeloop`
- `com.user.stocktglistener` if enabled

这些任务是盘中常驻进程或长时间轮询，不是短时 LLM jobs。保留在 launchd 可以避免让 Codex agent 从 09:25 到 15:00 持续运行。

## Legacy Claude launchd

旧版短时 LLM launchd jobs 已迁移为 Codex automations。确认 Codex jobs 已安装在 runtime host 后，可以禁用旧 Claude launchd jobs：

```bash
bash scripts/disable_legacy_claude_launchd.sh
```

旧模板保存在：

```bash
launchd/disabled/claude/
```

它们刻意不在 `launchd/com.user.stock*.plist` 路径下，避免安装 runtime services 时重新装回重复的 `claude -p` jobs。

## Verification

部署后检查三件事：

```bash
bash scripts/doctor_codex_runtime.sh
uv run pytest tests/test_docs_codex_migration.py -q
```

- Codex automations 位于 runtime host 的 `~/.codex/automations/`。
- 长时 daemon label 仍由 launchd 管理：`com.user.stockwatchloop`、`com.user.stockanomalyloop`、`com.user.stockthemeloop`。
- `launchd/disabled/claude/` 中的 legacy Claude jobs 不应被重新安装为 active jobs。

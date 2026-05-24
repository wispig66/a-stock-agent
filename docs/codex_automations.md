# Codex Automations Runbook

本文说明本机运行模型：短时 LLM jobs 放到本机 Codex automations，长时 watcher daemon 留在本机 launchd。当前机器就是交易 workflow 的 runtime。

## 部署模型

部署只保留本机路径：

- 本机安装 Python 依赖、Codex skills、Codex automations 和 launchd daemon。
- 本机 `~/.codex/automations/` 是短时 LLM jobs 的唯一调度来源。
- 本机 `~/Library/LaunchAgents/` 是长时 daemon 的唯一 launchd 来源。

## 本机安装

在本机执行：

```bash
cd /path/to/a-stock-agent

uv sync --group dev
cp .env.example .env
# 编辑 .env，填 Telegram bot token / chat_id 等运行参数

sqlite3 data/daily.db < stock_codex/schema/init_db.sql
uv run python -m stock_codex.tools.refresh_calendar

bash scripts/sync_codex_skills.sh
bash scripts/install_codex_automations.sh
bash scripts/install_runtime_services.sh
bash scripts/doctor_codex_runtime.sh
```

`scripts/install_codex_automations.sh` 只负责短时 LLM jobs；`scripts/install_runtime_services.sh` 负责长时 launchd daemon。

## Active Codex Jobs

这些 jobs 是短时 LLM workflow，由 Codex automations 调度：

| Job ID | Schedule | Task |
|---|---:|---|
| `stock-premarket` | Mon-Fri 08:00 | Run `stock-premarket` and push Telegram |
| `stock-intraday-09-30` | Mon-Fri 09:30 | Run `stock-intraday` current-time branch |
| `stock-intraday-09-45` | Mon-Fri 09:45 | Run `stock-intraday` current-time branch |
| `stock-intraday-11-30` | Mon-Fri 11:30 | Run `stock-intraday` current-time branch |
| `stock-intraday-14-30` | Mon-Fri 14:30 | Run `stock-intraday` current-time branch |
| `stock-postmarket` | Mon-Fri 15:35 | Run `stock-postmarket` and refresh daily data |
| `stock-weekly-review` | Sun 21:00 | Run `stock-weekly` |

Codex 在本机保存 automation 文件的位置：

```bash
~/.codex/automations/
```

## What Still Uses launchd

长时 watcher daemon 继续使用 launchd：

- `com.user.stockwatchloop`
- `com.user.stockanomalyloop`
- `com.user.stockthemeloop`
- `com.user.stocktglistener` if explicitly enabled

这些任务是盘中常驻进程或长时间轮询，不是短时 LLM jobs。保留在 launchd 可以避免让 Codex agent 从 09:25 到 15:00 持续运行。

## Legacy short LLM launchd

旧版短时 LLM launchd jobs 已迁移为 Codex automations。确认 Codex jobs 已安装在本机后，可以禁用旧 short-job launchd jobs：

```bash
bash scripts/disable_legacy_llm_launchd.sh
```

旧模板不再保存在仓库里，避免运行时安装脚本误装回重复 short-job launchd jobs。

## Verification

部署后检查三件事：

```bash
bash scripts/doctor_codex_runtime.sh
uv run pytest tests/test_docs_codex_migration.py tests/test_codex_runtime_scripts.py -q
```

- Codex automations 位于本机 `~/.codex/automations/`。
- 长时 daemon label 仍由 launchd 管理：`com.user.stockwatchloop`、`com.user.stockanomalyloop`、`com.user.stockthemeloop`。
- legacy short LLM jobs 不应被重新安装为 active launchd jobs。
- `doctor_codex_runtime.sh` 会在不发送 Telegram 消息的前提下检查 `api.telegram.org`、东财和同花顺域名解析，以及到 Telegram API 的 HTTPS 连通性；如果这里失败，当天盘前/盘中推送大概率也会失败或降级。

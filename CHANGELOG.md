# Changelog

记录较大版本变更。小修复直接看 git log。

## [Unreleased]

### T+1 awareness 改造 · Batch 1 数据基础 (2026-05-14)

为整套系统的 T+1 机制感知改造打地基。**本批次对用户可见行为无任何变化**，仅新增可调用工具与字段。后续 Batch 2 / 3 才会把这些能力接入 watch_loop 告警和 skill 文案。

**新增**
- `code/lib/calendar.py` — 交易日历查询模块（`is_trade_day` / `next_trade_day` / `trade_days_between` / `CalendarOutOfRange`）
- `code/lib/holdings.py` — 持仓状态机（`Holding` dataclass + `is_locked` + 加权均价合并 + 原子写）
- `code/refresh_calendar.py` — 拉 akshare 交易日历落地 `data/trade_calendar.csv`
- `tests/test_calendar.py` + `tests/test_holdings.py` — 14 个 TDD 用例
- `data/trade_calendar.csv` — 1990 → 2026-12-31 共 8797 个交易日
- `holdings.yaml` schema 新字段：`unlock_date`（T+1 解锁日，自动算）、`source`（录入来源）。老条目向后兼容
- `launchd/` 目录 — 4 个 plist 模板（`{{PROJECT_ROOT}}` 占位符）纳入仓库
- `scripts/setup.sh` — 一键安装：依赖 + 数据库 + 日历 + plist 渲染部署 + Telegram 验证
- `scripts/install_launchd.sh` — 单独重装 launchd 任务

**改动**
- `.claude/skills/stock-intraday/scripts/fetch_realtime.py` — `load_holdings` 转调 `lib.holdings`，统一数据来源，对下游保持 `list[dict]` 返回不变
- `pyproject.toml` — 加 `filelock>=3.13` 运行依赖；新增 dev group 含 `pytest>=8.0`；配置 `pythonpath = ["code"]`
- `holdings.yaml` header 注释 — 补 unlock_date / source / 历史兼容说明
- `.gitignore` — 加 `holdings.yaml.lock`

**设计/计划文档**
- `docs/superpowers/specs/2026-05-14-t-plus-1-awareness-design.md` — 全项目 T+1 改造 design
- `docs/superpowers/plans/2026-05-14-t-plus-1-batch1-data-foundation.md` — Batch 1 实施计划

**验证**
- `uv run pytest tests/` 全 14 个用例通过
- watch_loop --once 干跑无回归
- 端到端 upsert/remove/is_locked 流程符合预期

### 下一步
- Batch 2 · watch_loop 改造（sanity check 5 条 + 告警双轨分轨 + pending_signals.jsonl）—— 待 Batch 1 在生产跑 1-2 个交易日观察后启动
- Batch 3 · Telegram 入站 bot + 4 个 skill 文案 T+1 化

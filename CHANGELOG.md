# Changelog

记录较大版本变更。小修复直接看 git log。

## [Unreleased]

### TG 单股查询助手 · stock-query (2026-05-14)

新增「在 Telegram 发股票代码 → 30-90s 内回一张题材派决策卡」入口。常驻 daemon 10s 长轮询，前置校验秒级拒绝不合规票（科创/北交所/ST/未找到），合规票走 `claude -p` headless 跑新 skill `stock-query` 并把卡片流式写到 TG 同一条消息。

**新增**
- `.claude/skills/stock-query/SKILL.md` — 题材派 6 维判定 + fresh/holding 双分支模板 + 三档明确表态
- `scripts/tg_listener.py` — TG 长轮询守护进程，fcntl 文件锁串行化、1 跑 + 3 等队列、`subprocess` + `--output-format stream-json --include-partial-messages` 流式跑 skill，工具调用阶段也 editMessageText 显示进度
- `scripts/start_tg_listener.sh` / `stop_tg_listener.sh` — 交互式 shell 拉起 daemon（绕开 launchd + ~/Desktop 的 TCC 阻塞）
- `code/lib/query.py` — 单股查询数据层：`parse_input` / `board_of` / `is_st` / `lookup_by_name` + 联网拉 K 线 / 盘口 / 资金流 / 概念榜 / 新闻
- `scripts/refresh_stock_basic.py` — 全市场 sh_a + sz_a 每日刷新（Sina `Market_Center.getHQNodeData`，5202 票 / 259 ST），挂到 postmarket 流程末尾
- `code/run_tg_listener.sh` + `launchd/com.user.stocktglistener.plist` — KeepAlive 模板（项目搬出 Desktop 后可启用）
- `tests/test_query_lib.py`、`tests/test_query_lib_network.py`、`tests/test_refresh_stock_basic.py`、`tests/test_stock_basic_schema.py`、`tests/test_tg_listener.py` — 30+ TDD 用例
- `data/daily.db` 新增 `stock_basic` 表（code / name / board / is_st / list_date / updated_at）

**改动**
- `code/init_db.sql` — 追加 `stock_basic` DDL
- `code/run_postmarket.sh` — 主流程结尾调 `refresh_stock_basic.py`（失败不阻断）
- `README.md` — 新增「TG 单股查询」一节 + macOS TCC/FDA 注意事项

**设计 / 计划文档**
- `docs/superpowers/specs/2026-05-14-tg-stock-query-design.md`
- `docs/superpowers/plans/2026-05-14-tg-stock-query.md`

**已知限制**
- launchd 拉起的 daemon 在 `~/Desktop` 项目下会因 macOS TCC 在 `getcwd` 阻塞，需用 `scripts/start_tg_listener.sh` 在交互式 shell 中启动；若把项目搬到 `~/code` 之类无 TCC 限制的目录则可直接挂 launchd。
- 东财 push2.eastmoney.com 在本项目出口 IP 仍被风控拒，`fetch_concept_strength` / `fetch_money_flow` 实测会失败；skill 内已写好"数据缺失降一档"的降级路径。

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

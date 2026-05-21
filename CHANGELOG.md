# Changelog

记录较大版本变更。小修复直接看 git log。

## [Unreleased] 2026-05-17

### 周复盘 · stock-weekly (2026-05-17)

新增 L7 望远镜层。每周日 21:00 launchd 触发，输出 Part 1 本周复盘（6 节叙事 + 个人交易回顾）+ Part 2 下周方向（2-3 条主线 + 代表股 + 催化 + 风险，不给买点），落地长文 `data/weekly_review/YYYY-WW.md`。长文含 machine-readable YAML 块，L1 stock-premarket 加 Step 1.5 自动读取作为观察池先验种子。

新增模块：`code/lib/weekly_pack.py`、`.agents/skills/stock-weekly/`（SKILL.md + aggregate.py）、`scripts/weekly_loop.py`、`launchd/com.user.stockweekly.plist`。

设计 spec：`docs/superpowers/specs/2026-05-17-stock-weekly-design.md`。

## [Unreleased] 2026-05-15

### TG 随时分析助手 · stock-ask (2026-05-15)

新增「在 Telegram 发 `/ask <text>` 或 `/ask+ <text>` → 自动识别意图 + 输出板块/事件分析卡片」入口。补足 stock-query 单股之外的"随手丢一个东西问问"场景（板块名、政策事件、模糊描述都能进）。

**新增**
- `.agents/skills/stock-ask/SKILL.md` — 路由 + 板块卡 + 事件卡 + 模糊兜底
- `code/lib/intent.py` — 四层意图分类（显式覆盖 → 规则 → LLM → 模糊安全网）+ `build_sector_lexicon()` 从 `ths_hot_reason` / `limit_up.concept` 构建板块词库
- `code/lib/sector_pack.py` — `fuzzy_match` / `_load_lexicon` / `classify_stage`（启动期/主升期/高潮期/退潮期）/ `pick_top_n`（追高过滤 + 评分公式）/ `build_sector_pack`（4 面板 ThreadPool 并发）
- `code/lib/event_pack.py` — `calibrate`（题材库三档 ✓/△/✗）+ `build_event_pack`（normal/deep 双模式，deep 含 web 实时搜索补强 + 静默降级）
- `scripts/tg_listener.py` 扩展 — `/ask` / `/ask+` 解析、按模式分档超时（normal 180s / deep 300s）、入向 `tg_inbound` 落库 wrapper（log_inbound_start / update_parsed / finish）、`run_skill_streaming_generic` 通用流式跑 skill
- `scripts/set_tg_commands.py` — 注册 `/ask` 到 BotFather 菜单
- `scripts/migrate_tg_inbound.py` — 一次性 DB migration
- `data/daily.db` 新增 `tg_inbound` 表（update_id UNIQUE 去重 / parsed_payload JSON / response_msg_id 关联 / handler_status / duration_ms）+ 3 索引
- `tests/test_intent_classify.py`、`tests/test_sector_pack.py`、`tests/test_sector_pack_panels.py`、`tests/test_event_pack.py`、`tests/test_tg_inbound_schema.py`、`tests/test_tg_inbound_log.py`、`tests/test_stock_ask_e2e.py` — 30+ 新增 TDD 用例（全套 134 PASS）

**设计 & 计划**
- `docs/superpowers/specs/2026-05-15-stock-ask-design.md` — design spec
- `docs/superpowers/plans/2026-05-15-stock-ask.md` — 9 task 实施计划

**关键设计取舍**
- 个股意图 fallthrough 到现有 `stock-query`，不重复造轮子
- 题材库校准防 LLM 瞎猜：A 股交易员真实题材分类落到 `ths_hot_reason` + `limit_up.concept` + 实时榜
- 散户不做空：风险板块仅列名提醒
- 全量入向审计：所有 TG 命令落 `tg_inbound`（含 update_id 去重，重复推送被跳过）
- 不破坏现有 5 个 skill 的任何文件，纯增量

## [Unreleased] 2026-05-14

### Added
- 组合层风控 Week 1：总仓位计算器 + L1 盘前预检横幅
  - `risk_config.yaml`（gitignore）+ `risk_config.example.yaml` 模板
  - `code/lib/risk.py`：`load_risk_config` / `compute_exposure` / `preflight_check` / `make_price_fn_from_df` / `fetch_spot_price_fn`
  - `.agents/skills/stock-premarket/scripts/preflight.py` CLI 入口
  - L1 卡片支持总仓位超额 ⚠️ 横幅 + 每只候选股"当前可用 X%"额度行
- watch_loop 锁仓期告警分轨：今日买入持仓 (today < unlock_date) 命中 hold_stop / hold_dump / hold_vol 时改文案为"🌙 锁仓中 · 明早处理"，alert_key 加 `_locked` 后缀与解锁版去重隔离
- L4 盘后卡片新增持仓题材集中度判定：⚠️ ≥60% / 🟡 ≥40% / ✅ <40% / 空仓跳过，与 3a 题材延续性联动给出"保持/减半"动作建议
- L4 盘后连亏心态提醒（轻量版）：每日跟踪持仓加权浮盈亏，连亏 ≥ 2 天在卡片顶部插入 🧊 心态提醒段（提示明日自觉首仓减半至 15%，浮盈日自动消失）；新增 `risk_state.yaml`（gitignore）+ `code/lib/loss_streak.py` + `risk_config` 加 `loss_day_threshold_pct` / `loss_streak_warn_threshold` 两阈值；L1 / preflight / risk.py 不动

### TG 单股查询助手 · stock-query (2026-05-14)

新增「在 Telegram 发股票代码 → 30-90s 内回一张题材派决策卡」入口。常驻 daemon 10s 长轮询，前置校验秒级拒绝不合规票（科创/北交所/ST/未找到），合规票走 `codex exec` headless 跑新 skill `stock-query` 并把卡片流式写到 TG 同一条消息。

**新增**
- `.agents/skills/stock-query/SKILL.md` — 题材派 6 维判定 + fresh/holding 双分支模板 + 三档明确表态
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
- `.agents/skills/stock-intraday/scripts/fetch_realtime.py` — `load_holdings` 转调 `lib.holdings`，统一数据来源，对下游保持 `list[dict]` 返回不变
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

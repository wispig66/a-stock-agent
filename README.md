# A 股短线 Codex 辅助系统

一套基于 Codex Skills 的 A 股短线交易辅助系统。**研究员模式 + 严格止损纪律**，不是黑盒自动化交易。

> ⚠️ 本项目仅为决策辅助 + 信号推送工具，不构成投资建议。股市有风险，自负盈亏。所有下单由用户手工执行。

## 设计哲学

- **不追求"全自动炒股"**：LLM 现阶段做不准题材判断；监管也不允许散户自动下单
- **题材轮动 + 严格止损**：走赵老哥派纪律，**不**沿用炒股养家"永不止损"
- **题材轮动 = 消息驱动**：所有 skill 都内置独立的消息面板块（财联社电报 / 政策快讯 / 产业链事件），每条新闻按"题材驱动 / 持仓利空 / 新主线候选"分类，不只看价量
- **双轨止损**：Codex 盘中推送 + 同花顺条件单本地托管，互为冗余
- **9:50 后未触发即放弃**：避免追高
- **持仓首仓 ≤ 30%**，总仓位看情绪阶段
- **盘中 4 时点架构**：09:30 / 09:45 / 11:30 / 14:30，**基于真实游资圈调研**而非凭空假设（09:45 和 14:30 是公认两大黄金时段，11:30 是消息消化窗口）

## 架构

四个时段对应四个 skill，共享一份数据底座：

```
   L1 盘前 08:00      L2 盘中 4 时点 + 阈值轮询      L4 盘后 15:35      L3 全市场异动
   交易决策漏斗       动作裁决 + 半日/尾盘叙事        决策评分+次日策略   触发器（待做）
   stock-premarket → stock-intraday + watch_loop →  stock-postmarket   stock-anomaly
        ✅                    ✅                          ✅                ⏳
        │                     │                          │
        └─────────────────────┴────────┬─────────────────┘
                                       │
                            ┌──────────┴─────────┐
                            │  fact pack 共享层   │   data/fact_pack/*.md
                            │  SQLite 历史数据    │   data/daily.db
                            │  push_log 推送日志  │
                            │  holdings.yaml 持仓 │
                            └────────────────────┘
```

L2 4 时点：09:30 / 09:45 纪律提醒卡（轻、状态机） + 11:30 / 14:30 叙事卡片（题材强弱 + 消息面 + 决策）。中间时段（10:00-11:30 / 13:00-14:30）由独立 `watch_loop.py` 90 秒轮询观察池+持仓，触发 ±5% / 放量 2x / 破止损 / 封板等 6 种告警时推 TG 简讯，不调 Codex。

## 已实现

### L1 · stock-premarket（盘前 08:00）

交易决策漏斗，输出**今日是否出手 + 主攻 / 潜伏 / 备选 / 禁买决策单**，不再让用户从 5-8 只观察池里自己猜：

- **派别 A 二板接力**：盘前给死价（昨封板 × 1.01）+ -3% 硬止损
- **派别 B 龙头补涨**：5 日线 ± 1% 缩量低吸
- **派别 C 超跌反弹**：前低 + 1% 或日内分时低点
- **派别 D 首板候选**：只标候选 + 触发条件，**不给硬价**
- **派别 E 事件预期埋伏**：消息催化明确但盘面未启动，只允许低吸小仓，不追高

每张盘前卡必须包含 `decision_tickets` 机器块并落库。盘中和盘后优先读这张决策单，而不是反解析 Telegram 文本。主攻最多 1 只，潜伏最多 2 只；没有好机会时明确空仓。**30 天解禁过滤**（占流通市值 > 5% 直接剔除）。情绪阶段判定（启动/加速/分歧/退潮/一致）严格按数据，禁用"积累中"逃避。

### L2 · stock-intraday（盘中 4 时点 + watch_loop）

**4 个固定时点**（基于游资圈实际调研，非凭印象）由 Codex automations 触发：

| 时点 | 类型 | 工作 |
|------|------|------|
| 09:30 | 纪律提醒（轻） | 开盘竞价/弱转强判断、夜间→现在突发消息扫描、观察池试盘信号 |
| 09:45 | 纪律提醒（重） | 全天最关键时段后格局、买卖位触发情况、9:50 前未触发即放弃 |
| 11:30 | 半日叙事卡片 | 上午题材强弱 + 上午新增消息热点 + 下午策略微调 |
| 14:30 | 尾盘叙事卡片 | 黄金半小时启动、当日完整消息汇总、明日预案预演 |

**watch_loop.py 中间时段补充**：09:25 launchd 启动，90 秒轮询，命中 6 种阈值任一即推 TG 简讯（不调 Codex，省 token）：
- 💥 持仓跌破止损 / 🚨 观察池跌破止损
- 🚀 观察池触发买点 / ✅ 涨停封板
- 💥 持仓砸盘（涨幅 ≤ -5%）/ 异动放量（|涨幅| ≥ 5% 且量比 ≥ 2）

同 code+kind 一会话只推一次，15:00 自动退出。

**输入双轨**：交易计划来自 `decision_tickets`（无数据时才回退解析 L1 推送），实盘持仓来自 `holdings.yaml`（手动维护，含 cost / stop_loss / take_profit / genre 等字段）。

### L4 · stock-postmarket（盘后 15:35）

不是数据堆叠，是**给"明早要做决策的人"看的叙事卡片**：

1. **今日市场怎么走**：4-6 句叙事讲因果链
2. **当日 + 晚间消息**：当日全日快讯 + 盘后突发公告 + 隔夜外围预期，每条按"延续利好 / 退潮利空 / 新主线信号 / 持仓利空"分类
3. **决策评分 + 持仓处理**：对今早 L1 的主攻 / 潜伏 / 备选 / 禁买逐只评分，再给持仓明早具体动作
4. **主线观察**：✅ 延续 / 🔻 退潮 / 🆕 新主线
5. **明日决策**：再战 / 减仓观望 / 空仓（三选一，含仓位指引）
6. **明早 L1 重点盯什么**：3-5 条可量化的盯点
7. **今日教训**：1-2 条今日暴露的具体规律（如"3 板封单 < 1 亿次日接力胜率低"），**不立刻改 L1**，累积 5-10 个交易日后人工汇总反复出现 ≥3 次的规律才升级 L1 硬约束

副作用：自动 UPSERT `sentiment_daily` 表，让下一日 L1 阶段判定拥有 10 日基线。

### TG 单股查询 · stock-query（全天候）

在 Telegram 直接发股票代码或名称（主板/创业板），常驻 daemon `scripts/tg_listener.py` 10s 长轮询 → 调 `codex exec` 跑 `stock-query` skill → 30–90s 内回一张题材派决策卡。

- **fresh 分支**：未持仓 → 三档明确表态 [买入 / 观察 / 回避]；观察档必须给"什么信号出现升级为买入"
- **holding 分支**：在 `holdings.yaml` 里的票自动切换为 [加仓 / 持有 / 减仓清仓]
- **前置拒绝**（不调 Codex，秒级回复）：科创板 / 北交所 / ST / 不存在代码
- **流式输出**：占位消息发出后通过 `editMessageText` 实时刷新——工具调用阶段显示 "⏳ 已用 12s · 第 3 步：查数据…"，模型写卡片阶段按 `text_delta` 逐段更新，最终 HTML 渲染落到同一条消息
- **并发**：fcntl 串行，最多 1 跑 + 3 等，第 5 个请求回"忙"
- **隔离**：只接受 `ALLOWED_CHAT_ID`（默认 = `TG_CHAT_ID`）的消息，其它 chat 静默

启动（**项目在 `~/Desktop` 下时只能用交互式启动**，见下方注意）：

```bash
bash scripts/start_tg_listener.sh    # nohup 后台，PID 写到 data/tg_listener.pid
bash scripts/stop_tg_listener.sh     # 停
```

`stock_basic` 表（代码 → 名称 / 板块 / ST 标志）由 `scripts/refresh_stock_basic.py` 每日刷新，已挂到 postmarket 流程末尾。

**注意（macOS TCC/FDA）**：launchd 模板 `launchd/disabled/com.user.stocktglistener.plist` 默认保持禁用，`scripts/install_runtime_services.sh` 不会自动注册它。原因：项目在 `~/Desktop` 下时 launchd 拉起的 `uv` 进程会在 `getcwd` 阻塞（Desktop 是受 TCC 保护的目录，背景守护进程拿不到 Full Disk Access），表现为 `uv` 卡死无子进程。如果你把项目搬到 `~/code` 之类无 TCC 限制的目录，可以保留模板在 disabled 路径，并显式执行 `ENABLE_TG_LISTENER_LAUNCHD=1 bash scripts/install_runtime_services.sh`；否则一律用 `start_tg_listener.sh` 在交互式终端里拉起（继承终端 TCC 权限）。

### TG 随时分析 · stock-ask（全天候）

在 Telegram 发 `/ask <text>` 或 `/ask+ <text>` 触发随时分析，自动识别意图（个股 / 板块 / 事件 / 模糊），并发拉 4 面板（情绪 / 消息 / 基本面 / 技术面），输出"值不值得参与 + 推荐标的 + 买点"卡片。

- **触发**：
  - `/ask 600519` → 个股，fallthrough 到 stock-query
  - `/ask 光伏怎么样` → 板块卡（阶段 + 龙头 + Top 3-5 跟随股 + 买点/止损）
  - `/ask 国常会批了储能补贴` → 事件卡（受益板块 ✓/△ 标注 + 直接推荐标的）
  - `/ask+ <text>` → deep 模式，叠加 web 实时搜索补强（超时 300s vs normal 180s）
  - `/ask sector=光伏` / `/ask stock=600519` / `/ask event=<text>` → 显式覆盖意图分类
- **意图分类**：四层 cascade —— 显式覆盖 → 规则匹配（6 位代码 / 已知板块名 / 事件关键词）→ LLM 兜底 → 模糊安全网（返回候选 A/B/C）
- **题材库校准**：事件意图先 LLM 猜受益板块，再用 `ths_hot_reason` 近 30 日 + `limit_up.concept` 历史校准，标 ✓ 完全命中 / △ 近似匹配 / ✗ 未验证。全 ✗ 时降档但仍出卡
- **审计落库**：所有入向 TG 命令落 `tg_inbound` 表（update_id 去重 / parsed_payload JSON / response_msg_id / handler_status / duration_ms），用于后续胜率回溯
- **不做空建议**：风险板块仅列名提醒，散户无融券权限

新增模块：`stock_codex/market/intent.py`（意图分类）、`stock_codex/market/sector_pack.py`（板块四面板 + Top N + 阶段）、`stock_codex/market/event_pack.py`（事件归类 + 校准 + normal/deep）、`.agents/skills/stock-ask/SKILL.md`（路由 + 卡片模板）。

设计 spec：`docs/superpowers/specs/2026-05-15-stock-ask-design.md`。

### 周复盘 · stock-weekly（周日 21:00 自动）

每周日晚 21:00，Codex automations 触发 `stock-weekly` 生成本周复盘 + 下周方向：

- **Part 1 本周复盘** — 情绪周期 / 主线 / 资金 / 情绪指标 / 题材轮动 / 个人交易回顾（6 节叙事）
- **Part 2 下周方向** — 2-3 条主线 + 代表股 + 关键催化 + 风险（不给买点，买点交给周一 L1）
- **输出** — TG 摘要卡 + 落地长文 `data/weekly_review/YYYY-WW.md`（含 machine-readable YAML 块）
- **L1 接入** — 周一开始 L1 stock-premarket 自动读最近一份周复盘 YAML，作为观察池先验种子

手动触发：在本机 Codex 中手动运行 `stock-weekly`。`uv run scripts/weekly_loop.py --force` 仅作为 legacy fallback wrapper 保留，不是默认调度入口。

新增模块：`stock_codex/market/weekly_pack.py`（数据聚合 + 长文渲染 + YAML 读写）、`.agents/skills/stock-weekly/`（SKILL.md + aggregate.py）、Codex automation job `stock-weekly-review`，以及 legacy fallback `scripts/weekly_loop.py`。

### 数据扩展层

抽取自 [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data)（Apache 2.0）的 5 个端点，封装在 `.agents/skills/stock-premarket/scripts/extras.py`：

| 端点 | 用途 | 状态 |
|------|------|------|
| 同花顺热点 reason tags | 题材归因（人工标注） | ✅ 接入 L1 |
| 全市场龙虎榜 | 净买额排名 + 上榜原因 | ✅ 接入 L1 |
| 解禁日历（90 天） | 观察池过滤 | ✅ 接入 L1 |
| mootdx 行情 + K 线 | 实时报价（TCP 7709） | ⏸️ 备用，L3 用 |
| 百度资金流 | 分钟级主力/散户 | ❌ 403 IP 风控 |

### Telegram 推送层

`stock_codex.infra.notify` 自动 markdown → Telegram HTML 渲染：

- 支持 `**bold**` `*italic*` `` `code` `` `# 标题` 与 raw `<b>` 透传
- markdown 表格自动转 bullet 列表
- 所有推送自动入 `push_log` 表，含完整 text + msg_id

### 第二轨止损

`docs/同花顺条件单教程.md` —— 双轨止损第二轨。条件单是**本地软件托管**，不依赖你看不看手机。

## T+1 awareness 改造（进行中）

A 股 T+1 机制（当日买入最早 T+1 才能卖）一直是项目盲区——盘中触发"破止损立即出"告警对今日新仓物理上不可执行。已启动三批次改造：

| 批次 | 状态 | 范围 |
|------|------|------|
| **Batch 1 · 数据基础** | ✅ 完成 (2026-05-14) | 交易日历、holdings 状态机、`unlock_date` / `source` 字段、向后兼容、14 个 TDD 用例 |
| **Batch 2 · watch_loop 改造** | 待启动 | 买点 sanity check（5 条规则）、告警双轨分轨（今日新仓→"明早预案"/可卖仓→"立即出"）、pending_signals 落地 |
| **Batch 3 · bot + skill 文案** | 待启动 | Telegram 入站 bot（`/buy /sell /list /cancel /setsl`）、4 个 skill 文案 T+1 化 |

完整方案见 `docs/superpowers/specs/2026-05-14-t-plus-1-awareness-design.md`。Batch 1 改动不改变任何用户可见行为，从 Batch 2 起会看到告警措辞变化。

## 调度

调度分两层：`Codex automations` 负责短时 LLM jobs，`launchd 运行长时 daemon`。本项目按本机运行模型部署：当前机器就是交易 workflow 的 runtime，Codex automations 和 launchd 都安装在本机。

| 类型 | 任务 | 时间 | 入口 |
|------|------|------|------|
| Codex automations | L1 盘前 | 工作日 08:00 | `stock-premarket` |
| Codex automations | L2 开盘纪律 | 工作日 09:30 | `stock-intraday-09-30` |
| Codex automations | L2 关键时段 | 工作日 09:45 | `stock-intraday-09-45` |
| Codex automations | L2 半日叙事 | 工作日 11:30 | `stock-intraday-11-30` |
| Codex automations | L2 尾盘叙事 | 工作日 14:30 | `stock-intraday-14-30` |
| Codex automations | L4 盘后 | 工作日 15:35 | `stock-postmarket` |
| Codex automations | 周复盘 | 周日 21:00 | `stock-weekly-review` |
| launchd 运行长时 daemon | watch_loop | 工作日 09:25-15:00 | `com.user.stockwatchloop` |
| launchd 运行长时 daemon | anomaly_loop | 工作日 09:25-15:00 | `com.user.stockanomalyloop` |
| launchd 运行长时 daemon | theme_loop | 工作日盘中 | `com.user.stockthemeloop` |
| launchd 运行长时 daemon | tg_listener（可选） | 按需常驻 | `com.user.stocktglistener` |

短时 LLM jobs 包括 L1 盘前、L2 盘中 4 时点、L4 盘后、周复盘；长时 watcher daemon 包括 `watch_loop`、`anomaly_loop`、`theme_loop`，可选 `tg_listener`。只有两个前提：本机不睡眠（接电源时禁止自动睡眠）+ 网络通。

## 安装

需要：[uv](https://docs.astral.sh/uv/)（Python 包管理器，比 pip 快 10-50 倍）和 Codex。本机就是唯一 runtime：在本机同步依赖、安装 Codex automations、安装 launchd 服务并运行诊断。

### 本机安装

在本机执行：

```bash
# 1. 装 uv（如未安装）
brew install uv                                  # macOS
# 或 curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 进入仓库
cd /Users/wispig/Desktop/stock

# 3. 同步依赖（uv 自动下 Python 3.11、建 .venv、装 akshare/pandas/requests/PyYAML/filelock）
uv sync --group dev    # 含 pytest，用于跑测试

# 4. 复制 .env.example → .env，填入 Telegram bot token / chat_id
cp .env.example .env
# 编辑 .env

# 5. 初始化 SQLite
sqlite3 data/daily.db < stock_codex/schema/init_db.sql

# 6. 拉交易日历（T+1 unlock_date 计算依赖）
uv run python -m stock_codex.tools.refresh_calendar

# 7. 同步 Codex skills 并安装短时 LLM automations
bash scripts/sync_codex_skills.sh
bash scripts/install_codex_automations.sh

# 8. 安装长时 runtime services（launchd daemon）
bash scripts/install_runtime_services.sh

# 9. 诊断 runtime
bash scripts/doctor_codex_runtime.sh

# 10. （可选）跑测试
uv run pytest tests/
```

`scripts/setup.sh` 只作为本地基础初始化辅助保留；不要依赖它安装旧 launchd short jobs，也不要把 Telegram 推送验证视为 setup 的部署完成条件。短时 LLM 调度以 `scripts/install_codex_automations.sh` 为准，长时 daemon 以 `scripts/install_runtime_services.sh` 为准。

`scripts/doctor_codex_runtime.sh` 会检查 Telegram、东财、同花顺的 DNS/HTTPS 前置连通性，但不会发送真实 Telegram 消息。若这里失败，当天盘前/盘中任务即使执行，也可能出现卡片降级或 Telegram 未送达。

> 高级用户可通过 `UV_PYTHON=/path/to/python uv sync` 指定 Python 解释器；也可直接复用已有 conda/venv（用 `source xxx/bin/activate && pip install -e .`），但推荐 uv 路径。

### 风控配置

复制模板：

```bash
cp risk_config.example.yaml risk_config.yaml
```

字段：
- `total_capital`：总资金（现金 + 持仓市值基准），单位元
- `max_total_exposure_pct`：总仓位红线，超过 L1 卡片顶部出 ⚠️ 横幅
- `max_single_position_pct`：单只首仓硬上限

`risk_config.yaml` 含资金金额，已 gitignore。

## 目录结构

```
.
├── .agents/skills/
│   ├── stock-premarket/        # L1 skill
│   │   ├── SKILL.md
│   │   └── scripts/
│   │       ├── fetch_data.py   # fact pack 生成器
│   │       ├── extras.py       # 5 个扩展数据端点
│   │       └── push.py
│   ├── stock-intraday/         # L2 skill（盘中 4 时点）
│   │   ├── SKILL.md
│   │   └── scripts/
│   │       ├── fetch_realtime.py  # 观察池 + 持仓 + 实时行情
│   │       └── watch_loop.py      # 90s 阈值轮询（独立后台）
│   └── stock-postmarket/       # L4 skill
│       ├── SKILL.md
│       └── scripts/
│           └── fetch_postmarket.py
├── stock_codex/                   # 可安装 Python 包（共享逻辑 + app/tool 实现）
│   ├── infra/                     # SQLite / logger / Telegram notify
│   ├── domain/                    # 交易日历 / 持仓 / 决策票 / 风控
│   ├── market/                    # 行情 / 题材 / 事件 / 周报 / 卡片校验
│   ├── apps/                      # tg_listener + ask/query/weekly/theme app 实现
│   ├── tools/                     # review / refresh_calendar / download_daily 等人工工具
│   └── schema/init_db.sql         # SQLite schema
├── bin/                           # shell runtime entrypoints（launchd/manual fallback）
│   ├── run_premarket.sh
│   ├── run_intraday.sh
│   ├── run_watch_loop.sh
│   └── run_postmarket.sh
├── launchd/                       # 长时 daemon plist 模板（{{PROJECT_ROOT}} 占位符）
├── scripts/
│   ├── setup.sh                   # 本地基础初始化辅助
│   ├── install_codex_automations.sh
│   └── install_runtime_services.sh
├── tests/                         # pytest 单测（uv run pytest tests/）
├── holdings.yaml                  # 实盘持仓清单（手动维护，含 unlock_date / source 字段）
├── docs/
│   ├── 同花顺条件单教程.md          # 双轨止损第二轨
│   └── superpowers/
│       ├── specs/                 # 设计文档
│       └── plans/                 # 实施计划
├── research/                      # 18 份一手资料（情绪周期/游资语录/数据工具/合规）
├── CHANGELOG.md                   # 较大版本变更
└── data/                          # daily.db / fact_pack / trade_calendar.csv 等本地运行态（gitignored）
```

## 数据源

| 源 | 协议 | 用途 |
|----|------|------|
| akshare | HTTPS | 涨停池 / 炸板池 / 跌停池 / 解禁 |
| 同花顺 zx.10jqka | HTTP | 强势股 reason tags |
| 东财 datacenter | HTTPS | 全市场龙虎榜 |
| mootdx | TCP 7709 | 实时行情 / K 线 |
| 财联社 cls.cn | HTTPS | 新闻 |

**已知问题**：东财 eastmoney.com 部分子域名在某些代理环境（127.0.0.1 Clash 规则模式）会被拒，已切换至同花顺 / 新浪 / 腾讯备份源。百度股市通 PAE 接口需要 cookie，目前 403。

## 致谢

- [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) —— 5 个数据端点抽取（Apache 2.0）
- [akshare](https://github.com/akfamily/akshare) —— 主力数据源
- [mootdx](https://github.com/mootdx/mootdx) —— 通达信 TCP 行情

## License

MIT。`extras.py` 内嵌的 a-stock-data 派生代码保留 Apache 2.0 attribution。

## Disclaimer

本项目仅供学习研究，不构成任何投资建议。使用本项目产生的任何收益或损失由使用者自行承担。所有交易由使用者手工执行，本系统不代为下单。

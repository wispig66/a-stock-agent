# A 股短线 CC 辅助系统

一套基于 [Claude Code](https://www.anthropic.com/claude-code) Skills 的 A 股短线交易辅助系统。**研究员模式 + 严格止损纪律**，不是黑盒自动化交易。

> ⚠️ 本项目仅为决策辅助 + 信号推送工具，不构成投资建议。股市有风险，自负盈亏。所有下单由用户手工执行。

## 设计哲学

- **不追求"全自动炒股"**：LLM 现阶段做不准题材判断；监管也不允许散户自动下单
- **题材轮动 + 严格止损**：走赵老哥派纪律，**不**沿用炒股养家"永不止损"
- **题材轮动 = 消息驱动**：所有 skill 都内置独立的消息面板块（财联社电报 / 政策快讯 / 产业链事件），每条新闻按"题材驱动 / 持仓利空 / 新主线候选"分类，不只看价量
- **双轨止损**：CC 盘中推送 + 同花顺条件单本地托管，互为冗余
- **9:50 后未触发即放弃**：避免追高
- **持仓首仓 ≤ 30%**，总仓位看情绪阶段
- **盘中 4 时点架构**：09:30 / 09:45 / 11:30 / 14:30，**基于真实游资圈调研**而非凭空假设（09:45 和 14:30 是公认两大黄金时段，11:30 是消息消化窗口）

## 架构

四个时段对应四个 skill，共享一份数据底座：

```
   L1 盘前 08:30      L2 盘中 4 时点 + 阈值轮询      L4 盘后 15:35      L3 全市场异动
   研究员模式         纪律 + 半日/尾盘叙事            复盘+次日策略       触发器（待做）
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

L2 4 时点：09:30 / 09:45 纪律提醒卡（轻、状态机） + 11:30 / 14:30 叙事卡片（题材强弱 + 消息面 + 决策）。中间时段（10:00-11:30 / 13:00-14:30）由独立 `watch_loop.py` 90 秒轮询观察池+持仓，触发 ±5% / 放量 2x / 破止损 / 封板等 6 种告警时推 TG 简讯，不调 CC。

## 已实现

### L1 · stock-premarket（盘前 08:30）

研究员模式，输出**今日观察池 5-8 只 + 每只按 4 派别标记买卖纪律**：

- **派别 A 二板接力**：盘前给死价（昨封板 × 1.01）+ -3% 硬止损
- **派别 B 龙头补涨**：5 日线 ± 1% 缩量低吸
- **派别 C 超跌反弹**：前低 + 1% 或日内分时低点
- **派别 D 首板候选**：只标候选 + 触发条件，**不给硬价**

每只候选必须包含 **代码 + 名称 + 行业 + 买点 + 止盈 + 止损 + 仓位**。**30 天解禁过滤**（占流通市值 > 5% 直接剔除）。情绪阶段判定（启动/加速/分歧/退潮/一致）严格按数据，禁用"积累中"逃避。

### L2 · stock-intraday（盘中 4 时点 + watch_loop）

**4 个固定时点**（基于游资圈实际调研，非凭印象）由 launchd 自动触发：

| 时点 | 类型 | 工作 |
|------|------|------|
| 09:30 | 纪律提醒（轻） | 开盘竞价/弱转强判断、夜间→现在突发消息扫描、观察池试盘信号 |
| 09:45 | 纪律提醒（重） | 全天最关键时段后格局、买卖位触发情况、9:50 前未触发即放弃 |
| 11:30 | 半日叙事卡片 | 上午题材强弱 + 上午新增消息热点 + 下午策略微调 |
| 14:30 | 尾盘叙事卡片 | 黄金半小时启动、当日完整消息汇总、明日预案预演 |

**watch_loop.py 中间时段补充**：09:25 launchd 启动，90 秒轮询，命中 6 种阈值任一即推 TG 简讯（不调 CC，省 token）：
- 💥 持仓跌破止损 / 🚨 观察池跌破止损
- 🚀 观察池触发买点 / ✅ 涨停封板
- 💥 持仓砸盘（涨幅 ≤ -5%）/ 异动放量（|涨幅| ≥ 5% 且量比 ≥ 2）

同 code+kind 一会话只推一次，15:00 自动退出。

**输入双轨**：观察池来自 L1 推送（解析 `push_log` 当日记录），实盘持仓来自 `holdings.yaml`（手动维护，含 cost / stop_loss / take_profit / genre 等字段）。

### L4 · stock-postmarket（盘后 15:35）

不是数据堆叠，是**给"明早要做决策的人"看的叙事卡片**：

1. **今日市场怎么走**：4-6 句叙事讲因果链
2. **当日 + 晚间消息**：当日全日快讯 + 盘后突发公告 + 隔夜外围预期，每条按"延续利好 / 退潮利空 / 新主线信号 / 持仓利空"分类
3. **持仓处理**：对今早 L1 观察池假设买入的票，逐只给"已触发/假突破/未触发/已止损"5 种状态的明早具体动作
4. **主线观察**：✅ 延续 / 🔻 退潮 / 🆕 新主线
5. **明日决策**：再战 / 减仓观望 / 空仓（三选一，含仓位指引）
6. **明早 L1 重点盯什么**：3-5 条可量化的盯点
7. **今日教训**：1-2 条今日暴露的具体规律（如"3 板封单 < 1 亿次日接力胜率低"），**不立刻改 L1**，累积 5-10 个交易日后人工汇总反复出现 ≥3 次的规律才升级 L1 硬约束

副作用：自动 UPSERT `sentiment_daily` 表，让下一日 L1 阶段判定拥有 10 日基线。

### TG 单股查询 · stock-query（全天候）

在 Telegram 直接发股票代码或名称（主板/创业板），常驻 daemon `scripts/tg_listener.py` 10s 长轮询 → 调 `claude -p` 跑 `stock-query` skill → 30–90s 内回一张题材派决策卡。

- **fresh 分支**：未持仓 → 三档明确表态 [买入 / 观察 / 回避]；观察档必须给"什么信号出现升级为买入"
- **holding 分支**：在 `holdings.yaml` 里的票自动切换为 [加仓 / 持有 / 减仓清仓]
- **前置拒绝**（不调 CC，秒级回复）：科创板 / 北交所 / ST / 不存在代码
- **流式输出**：占位消息发出后通过 `editMessageText` 实时刷新——工具调用阶段显示 "⏳ 已用 12s · 第 3 步：查数据…"，模型写卡片阶段按 `text_delta` 逐段更新，最终 HTML 渲染落到同一条消息
- **并发**：fcntl 串行，最多 1 跑 + 3 等，第 5 个请求回"忙"
- **隔离**：只接受 `ALLOWED_CHAT_ID`（默认 = `TG_CHAT_ID`）的消息，其它 chat 静默

启动（**项目在 `~/Desktop` 下时只能用交互式启动**，见下方注意）：

```bash
bash scripts/start_tg_listener.sh    # nohup 后台，PID 写到 data/tg_listener.pid
bash scripts/stop_tg_listener.sh     # 停
```

`stock_basic` 表（代码 → 名称 / 板块 / ST 标志）由 `scripts/refresh_stock_basic.py` 每日刷新，已挂到 postmarket 流程末尾。

**注意（macOS TCC/FDA）**：launchd 模板 `launchd/com.user.stocktglistener.plist` 已入库，但项目在 `~/Desktop` 下时 launchd 拉起的 `uv` 进程会在 `getcwd` 阻塞（Desktop 是受 TCC 保护的目录，背景守护进程拿不到 Full Disk Access）。若你把项目搬到 `~/code` 之类无 TCC 限制的目录，`bash scripts/install_launchd.sh` 即可挂载常驻；否则用上面的 `start_tg_listener.sh` 在交互式 shell 里拉起（终端 inheritance 自带 TCC 权限）。

### 数据扩展层

抽取自 [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data)（Apache 2.0）的 5 个端点，封装在 `.claude/skills/stock-premarket/scripts/extras.py`：

| 端点 | 用途 | 状态 |
|------|------|------|
| 同花顺热点 reason tags | 题材归因（人工标注） | ✅ 接入 L1 |
| 全市场龙虎榜 | 净买额排名 + 上榜原因 | ✅ 接入 L1 |
| 解禁日历（90 天） | 观察池过滤 | ✅ 接入 L1 |
| mootdx 行情 + K 线 | 实时报价（TCP 7709） | ⏸️ 备用，L3 用 |
| 百度资金流 | 分钟级主力/散户 | ❌ 403 IP 风控 |

### Telegram 推送层

`code/notify.py` 自动 markdown → Telegram HTML 渲染：

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

macOS launchd 全自动触发（电脑开机 + 已登录即可，**不需要保持任何会话**）：

| 任务 | 时间（工作日） | plist |
|------|---------------|-------|
| L1 盘前 | 08:30 | `com.user.stockpremarket.plist` |
| watch_loop 启动 | 09:25 | `com.user.stockwatchloop.plist`（自动 15:00 退出） |
| L2 开盘纪律 | 09:30 | `com.user.stockintraday.plist` |
| L2 关键时段 | 09:45 | 同上（4 时点共用一份 plist，内部按时间路由） |
| L2 半日叙事 | 11:30 | 同上 |
| L2 尾盘叙事 | 14:30 | 同上 |
| L4 盘后 | 15:35 | `com.user.stockpostmarket.plist` |

只有两个前提：电脑不睡眠（接电源时禁止自动睡眠）+ 网络通。

## 安装

需要：[uv](https://docs.astral.sh/uv/)（Python 包管理器，比 pip 快 10-50 倍）和 [Claude Code](https://www.anthropic.com/claude-code) CLI。

### 一键安装（推荐）

新机迁移或初次安装：

```bash
# 1. 装 uv（如未安装）
brew install uv                                  # macOS
# 或 curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. clone 仓库后跑 setup
git clone https://github.com/wispig66/a-stock-agent.git stock
cd stock
bash scripts/setup.sh
```

`scripts/setup.sh` 会自动完成：依赖同步、SQLite 初始化、akshare 交易日历拉取、`.env` 模板生成（首次跑会提示你填 token 后重跑）、launchd plist 渲染部署、Telegram 推送验证。**幂等**，重复跑不会破坏现有状态。

### 手动逐步安装

```bash
# 1. 同步依赖（uv 自动下 Python 3.11、建 .venv、装 akshare/pandas/requests/PyYAML/filelock）
uv sync --group dev    # 含 pytest，用于跑测试

# 2. 复制 .env.example → .env，填入 Telegram bot token / chat_id
cp .env.example .env
# 编辑 .env

# 3. 初始化 SQLite
sqlite3 data/daily.db < code/init_db.sql

# 4. 拉交易日历（T+1 unlock_date 计算依赖）
uv run python code/refresh_calendar.py

# 5. 测试 Telegram 推送
uv run code/notify.py test

# 6. 安装 launchd 任务
bash scripts/install_launchd.sh

# 7. 手动跑一次 L1（不等 08:30）
bash code/run_premarket.sh

# 8. （可选）跑测试
uv run pytest tests/
```

> 高级用户可通过 `UV_PYTHON=/path/to/python uv sync` 指定 Python 解释器；也可直接复用已有 conda/venv（用 `source xxx/bin/activate && pip install -e .`），但推荐 uv 路径。

## 目录结构

```
.
├── .claude/skills/
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
├── code/
│   ├── lib/                       # 共享工具模块（T+1 改造 Batch 1 引入）
│   │   ├── calendar.py            # 交易日历查询（is_trade_day / next_trade_day）
│   │   └── holdings.py            # 持仓状态机（Holding + is_locked + 加权均价）
│   ├── notify.py                  # Telegram + md→HTML 渲染
│   ├── init_db.sql                # SQLite schema
│   ├── refresh_calendar.py        # 拉 akshare 交易日历到 data/trade_calendar.csv
│   ├── run_premarket.sh           # launchd entry
│   ├── run_intraday.sh            # L2 launchd entry（4 时点共用）
│   ├── run_watch_loop.sh          # watch_loop launchd entry
│   └── run_postmarket.sh
├── launchd/                       # launchd plist 模板（{{PROJECT_ROOT}} 占位符）
├── scripts/
│   ├── setup.sh                   # 一键新机安装
│   └── install_launchd.sh         # 单独重装 launchd 任务
├── tests/                         # pytest 单测（uv run pytest tests/）
├── holdings.yaml                  # 实盘持仓清单（手动维护，含 unlock_date / source 字段）
├── docs/
│   ├── 同花顺条件单教程.md          # 双轨止损第二轨
│   └── superpowers/
│       ├── specs/                 # 设计文档
│       └── plans/                 # 实施计划
├── research/                      # 18 份一手资料（情绪周期/游资语录/数据工具/合规）
├── A股短线CC方案.md                # 1.4 万字 Canonical 长文
├── CHANGELOG.md                   # 较大版本变更
└── data/                          # daily.db + fact_pack + trade_calendar.csv（部分 gitignored）
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

- [Anthropic Claude Code](https://www.anthropic.com/claude-code) —— skill 框架
- [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) —— 5 个数据端点抽取（Apache 2.0）
- [akshare](https://github.com/akfamily/akshare) —— 主力数据源
- [mootdx](https://github.com/mootdx/mootdx) —— 通达信 TCP 行情

## License

MIT。`extras.py` 内嵌的 a-stock-data 派生代码保留 Apache 2.0 attribution。

## Disclaimer

本项目仅供学习研究，不构成任何投资建议。使用本项目产生的任何收益或损失由使用者自行承担。所有交易由使用者手工执行，本系统不代为下单。

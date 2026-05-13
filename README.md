# A 股短线 CC 辅助系统

一套基于 [Claude Code](https://www.anthropic.com/claude-code) Skills 的 A 股短线交易辅助系统。**研究员模式 + 严格止损纪律**，不是黑盒自动化交易。

> ⚠️ 本项目仅为决策辅助 + 信号推送工具，不构成投资建议。股市有风险，自负盈亏。所有下单由用户手工执行。

## 设计哲学

- **不追求"全自动炒股"**：LLM 现阶段做不准题材判断；监管也不允许散户自动下单
- **题材轮动 + 严格止损**：走赵老哥派纪律，**不**沿用炒股养家"永不止损"
- **双轨止损**：CC 盘中推送 + 同花顺条件单本地托管，互为冗余
- **9:50 后未触发即放弃**：避免追高
- **持仓首仓 ≤ 30%**，总仓位看情绪阶段

## 架构

四个时段对应四个 skill，共享一份数据底座：

```
   L1 盘前 08:30          L2 竞价 09:25           L3 盘中触发           L4 盘后 15:35
   研究员模式             收敛筛选               触发器模式            复盘+次日策略
   stock-premarket   →   stock-bidding   →   stock-intraday   →   stock-postmarket
        ✅ 已实现              ⏳ 待做              ⏳ 待做              ✅ 已实现
        │                       │                       │                       │
        └───────────────────────┴───────────┬───────────┴───────────────────────┘
                                            │
                                  ┌─────────┴─────────┐
                                  │  fact pack 共享层  │   data/fact_pack/*.md
                                  │  SQLite 历史数据   │   data/daily.db
                                  │  push_log 推送日志 │
                                  └───────────────────┘
```

## 已实现

### L1 · stock-premarket（盘前 08:30）

研究员模式，输出**今日观察池 5-8 只 + 每只按 4 派别标记买卖纪律**：

- **派别 A 二板接力**：盘前给死价（昨封板 × 1.01）+ -3% 硬止损
- **派别 B 龙头补涨**：5 日线 ± 1% 缩量低吸
- **派别 C 超跌反弹**：前低 + 1% 或日内分时低点
- **派别 D 首板候选**：只标候选 + 触发条件，**不给硬价**

每只候选必须包含 **代码 + 名称 + 行业 + 买点 + 止盈 + 止损 + 仓位**。**30 天解禁过滤**（占流通市值 > 5% 直接剔除）。情绪阶段判定（启动/加速/分歧/退潮/一致）严格按数据，禁用"积累中"逃避。

### L4 · stock-postmarket（盘后 15:35）

不是数据堆叠，是**给"明早要做决策的人"看的叙事卡片**：

1. **今日市场怎么走**：4-6 句叙事讲因果链
2. **持仓处理**：对今早 L1 观察池假设买入的票，逐只给"已触发/假突破/未触发/已止损"5 种状态的明早具体动作
3. **主线观察**：✅ 延续 / 🔻 退潮 / 🆕 新主线
4. **明日决策**：再战 / 减仓观望 / 空仓（三选一，含仓位指引）
5. **明早 L1 重点盯什么**：3-5 条可量化的盯点

副作用：自动 UPSERT `sentiment_daily` 表，让下一日 L1 阶段判定拥有 10 日基线。

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

## 调度

macOS launchd 自动触发：

| 任务 | 时间 | plist |
|------|------|-------|
| L1 盘前 | 工作日 08:30 | `~/Library/LaunchAgents/com.user.stockpremarket.plist` |
| L4 盘后 | 工作日 15:35 | `~/Library/LaunchAgents/com.user.stockpostmarket.plist` |

## 安装

```bash
# 1. 创建 conda 环境
conda create -n stock python=3.11
conda activate stock
pip install akshare mootdx pandas requests

# 2. 复制 .env.example → .env，填入 Telegram bot token / chat_id
cp .env.example .env
# 编辑 .env

# 3. 初始化 SQLite
sqlite3 data/daily.db < code/init_db.sql

# 4. 测试 Telegram 推送
python code/notify.py test

# 5. 手动跑一次 L1（不等 08:30）
bash code/run_premarket.sh
```

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
│   └── stock-postmarket/       # L4 skill
│       ├── SKILL.md
│       └── scripts/
│           └── fetch_postmarket.py
├── code/
│   ├── notify.py               # Telegram + md→HTML 渲染
│   ├── init_db.sql             # SQLite schema
│   ├── run_premarket.sh        # launchd entry
│   └── run_postmarket.sh
├── docs/
│   └── 同花顺条件单教程.md       # 双轨止损第二轨
├── research/                   # 18 份一手资料（情绪周期/游资语录/数据工具/合规）
├── A股短线CC方案.md             # 1.4 万字 Canonical 长文
└── data/                       # daily.db + fact_pack 输出（gitignored）
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

# TG 单股查询助手 设计

日期：2026-05-14
作者：wispig + Claude Code

## 1. 目标

在 Telegram 上发送一只 A 股代码或名称，Claude Code 在 30–90 秒内回一张决策卡片：

- 题材派视角的"值不值得买"判断
- 三档明确表态：买入 / 观察 / 回避（观察档必须给"升级为买入"的信号）
- 关键价位：买点、止损位、止盈位（第一/第二目标）
- 持仓票自动切换为"加仓 / 持有 / 减仓清仓"分支

## 2. 范围

### In scope
- 全天候（盘中/盘后/周末都能问）
- 沪深主板 + 创业板
- 已持仓票自动识别并切换决策框架
- ST / 停牌 / 不支持板块 → 前置拒绝卡，不调 CC

### Out of scope (YAGNI)
- 科创板（688\*）、北交所（8\*/4\*）
- 多用户 / 多 chat_id
- Webhook 模式
- 缓存层
- 自定义命令（/help、/list 等）
- 异步并发分析（文件锁串行）

## 3. 架构

```
TG 用户消息
    │ (10s 轮询 getUpdates)
    ▼
scripts/tg_listener.py  ── 常驻进程，launchd KeepAlive
    │
    ├─ 解析: "600519" / "贵州茅台" / "茅台" → 标准化 code+name
    ├─ 校验: 主板/创业板？非 ST？非停牌？→ 否则回拒绝卡，不调 CC
    ├─ 判定: code ∈ holdings.yaml？ → mode=holding；否则 mode=fresh
    ▼
claude -p （headless，bypassPermissions，stdin 传 prompt）
    skill: stock-query
    入参: code, mode
    ▼
.claude/skills/stock-query/SKILL.md
    │ 经 code/lib/query.py 拉数据
    │ 走题材派决策框架
    │ 出三档结论 + 升级条件 + 关键价位
    ▼
markdown 输出 → tg_listener 用 notify.push_md 回原 chat_id
    │
    └─ 全程 log 到 SQLite push_log 表（复用现有）
```

### 关键约束
- 一个新 skill `stock-query` + 一个新守护进程 `tg_listener.py`，不动现有 4 个 skill
- 复用 `code/notify.py`、`code/db.py`、`code/lib/holdings.py`
- 文件锁 `/tmp/stock-query.lock` 保证串行，排队>3 直接拒绝
- 进程崩溃由 launchd KeepAlive 拉起；TG offset 持久化到 `data/tg_offset.txt`

## 4. 组件详细

### 4.1 `scripts/tg_listener.py`（新建）

长轮询 TG、解析消息、过滤、转发到 CC headless。

**主循环（伪代码）**：
```python
offset = load_last_offset()
while True:
    updates = tg.get_updates(offset=offset, timeout=10)
    for u in updates:
        offset = u.update_id + 1
        save_offset(offset)
        msg = u.message
        if msg.chat_id != ALLOWED_CHAT_ID:
            continue
        handle(msg.text)
```

**输入解析规则**：
1. 去空格、去 `$`、`#`、`SH`/`SZ` 前缀
2. 全数字 6 位 → 当代码
3. 中文 → 在本地 `stock_basic` 表 fuzzy match（精确包含；多结果 → 回"找到多只，请发代码：…"）
4. 解析失败 → 静默忽略（避免被群消息/闲聊触发）

**前置校验**：
| 情况 | 响应 |
|---|---|
| 科创板（688\*）/ 北交所（8\*/4\*） | 拒绝卡："暂不支持科创板/北交所" |
| ST / \*ST（看 stock_basic.is_st） | 拒绝卡："ST 票风险过高，建议回避" |
| 当日停牌 | 拒绝卡："今日停牌，跳过" |
| 不在 stock_basic | 拒绝卡："未找到该代码" |

**并发控制**：
- 文件锁 `/tmp/stock-query.lock`（fcntl）
- 队列容量 = 1 在跑 + 最多 3 等待 = 4；第 5 个及之后立即回"忙，稍后再问"
- 单次 CC 子进程超时 180s → 杀进程，回"分析超时"

**重试**：TG API 失败用指数退避（1s/2s/4s/8s/30s 封顶），不退出进程。

### 4.2 `.claude/skills/stock-query/SKILL.md`（新建）

入参（由 tg_listener 拼到 prompt 里）：`code=600519 mode=fresh|holding`

**数据拉取（走 `code/lib/query.py`）**：
1. 最近 60 个交易日 K 线（日线，DB 已有）
2. 当日实时盘口（新浪 `hq.sinajs.cn`，参考 `fetch_realtime`）
3. 同板块/同概念表现（东财概念板块；近 5/10/20 日涨幅 + 龙头）
4. 近 5 日资金流（同花顺主力净流入）
5. 近 7 天该股相关新闻（财联社 + 同花顺搜索，至少 3 条标题）
6. mode=holding 时额外从 holdings.yaml 读成本价、仓位、buy_date

**决策框架（题材派）**：

| 维度 | 判定 |
|---|---|
| 题材归属 | 该票主线概念 + 该概念近 5/10 日强度（板块涨幅排名） |
| 题材位置 | 启动期 / 主升期 / 高潮期 / 退潮期（看概念指数形态 + 龙头连板高度） |
| 个股位置 | 龙头 / 二线跟风 / 边缘补涨；距题材龙头多远 |
| 资金 | 近 3 日主力净流入方向 + 强度 |
| 技术位置 | 距 5/10/20 日均线位置、近期高低点、量能 |
| 消息面 | 近 7 日有无重大公告 / 利好利空 |

**fresh 分支三档**：

| 档 | 触发 |
|---|---|
| 买入 | 题材在启动/主升 + 个股是龙头或紧跟龙头 + 资金净流入 + 技术不在高位 |
| 观察 | 题材方向对但位置/资金/技术任一项不达标。必须列出"什么信号出现升级为买入" |
| 回避 | 题材退潮 / 高位滞涨 / 资金持续流出 / 技术破位 之一即触发 |

**holding 分支三档**：加仓 / 持有 / 减仓清仓；额外给"止损是否该上移"。

**关键价位（每档都必给）**：
- 买点（限价 or 触发条件）
- 止损位（破位价，给具体数字）
- 止盈位（第一目标 / 第二目标）

**降级**：任一数据源拉取失败 → 该项标"数据缺失"，结论降一档（买入→观察，观察→回避）。

### 4.3 `code/lib/query.py`（新建）

封装数据拉取逻辑，给 skill 调用。函数边界：

- `fetch_kline(code, days=60) -> DataFrame`
- `fetch_realtime(code) -> dict`
- `fetch_concept_strength(code) -> dict`（含主线概念名/排名/龙头）
- `fetch_money_flow(code, days=5) -> DataFrame`
- `fetch_recent_news(code, days=7) -> list[dict]`
- `is_st(code) -> bool`、`is_suspended(code) -> bool`、`board_of(code) -> str`

### 4.4 `scripts/refresh_stock_basic.py`（新建）

每日 17:00 跑一次（挂到现有 postmarket 流程后或独立 launchd），写入 `stock_basic` 表：

| 字段 | 说明 |
|---|---|
| code | 6 位代码（主键） |
| name | 当前股票名称 |
| board | main / chinext / star / bse |
| list_date | 上市日 |
| is_st | 0/1（名称含 ST 或 \*ST 或交易所标志位） |
| updated_at | 刷新时间 |

数据源：东财 / 同花顺 stock_basic 接口（任选一可用）。

### 4.5 `code/init_db.sql`（更新）

新增 `stock_basic` 表 DDL。

### 4.6 `launchd/com.stock.tg_listener.plist`（新建）

```xml
<key>KeepAlive</key><true/>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>.../logs/tg_listener.out.log</string>
<key>StandardErrorPath</key><string>.../logs/tg_listener.err.log</string>
```

## 5. 卡片格式

### fresh 分支
```
📊 贵州茅台 600519  [买入 / 观察 / 回避]
━━━━━━━━━━━━━━━━
🎯 结论：观察（暂不建议追）
理由：白酒板块处退潮中段，资金连续3日净流出，
     虽公司基本面稳，但短线缺催化。

🏷 题材：白酒消费 · 退潮期 · 板块5日-2.3%
📍 位置：板块二线（龙头泸州老窖，相对落后4%）
💰 资金：近3日主力净流出1.2亿
📈 技术：跌破10日线，量能萎缩
📰 消息：无重大利好/利空（近7日）

⚡ 升级为"买入"的信号（满足任一）：
  · 白酒板块单日涨幅>2%且龙头领涨
  · 主力资金转为净流入且持续2日
  · 站回20日线且放量

💵 关键价位
  买点：1620 触发（站回20日线放量）
  止损：1545（前低破位）
  止盈：1720 / 1800
━━━━━━━━━━━━━━━━
⚠️ 短线纪律：观察档≠可建仓，等信号
```

### holding 分支
```
📊 贵州茅台 600519  [加仓 / 持有 / 减仓清仓]
━━━━━━━━━━━━━━━━
🎯 结论：减仓1/2
持仓：1580成本 · 5日前买入 · 当前1605（+1.6%）

🏷 题材：白酒消费 · 退潮期
💰 资金：近3日主力净流出1.2亿
📈 技术：MACD死叉将形成

⚠️ 触发减仓的逻辑：
  · 题材已退潮，赚的钱要落袋
  · 资金持续流出
  · 你买入逻辑（板块启动跟车）已失效

🛡 剩余1/2止损上移到 1580（成本价保本）
🎯 反弹到 1640 全清
━━━━━━━━━━━━━━━━
```

### 拒绝卡
```
❌ 600519
原因：ST 票风险过高，本助手不分析
```

### 格式约束
- TG Markdown（复用 `notify.push_md`），不用 HTML
- 单条 ≤ 800 字
- emoji 只作分区标记

## 6. 错误处理矩阵

| 场景 | 行为 |
|---|---|
| TG API 网络抖动 | 指数退避（1/2/4/8/30s 封顶），不退出 |
| 数据源拉取失败（任一） | 该项标"数据缺失"，结论降一档 |
| CC headless 超时 >180s | 杀子进程，回"分析超时，稍后再试" |
| 队列已满（1 跑 + 3 等） | 第 5 个及之后立即回"忙，稍后再问" |
| 解析失败 / 非股票内容 | 静默忽略 |
| 进程崩溃 | launchd KeepAlive 拉起；offset 持久化避免漏消息 |

## 7. 测试

**单元测试**（`tests/test_tg_listener.py`、`tests/test_query.py`）：
- 输入解析：代码 / 中文 / 带前缀 / ST / 科创板 / 北交所 / 不存在代码
- stock_basic 查询：fuzzy match 多结果分支
- holdings 命中判断

**集成测试**：mock TG API + mock CC 子进程，验证完整收发链路。

**实测**：用测试 chat（非主 chat）跑 5 只票——主板非持仓、创业板、持仓、ST、不存在代码。

## 8. 部署

### 新文件
- `.claude/skills/stock-query/SKILL.md`
- `code/lib/query.py`
- `scripts/tg_listener.py`
- `scripts/refresh_stock_basic.py`
- `launchd/com.stock.tg_listener.plist`
- `tests/test_tg_listener.py`、`tests/test_query.py`

### 修改文件
- `code/init_db.sql`（加 `stock_basic` 表）
- `scripts/install_launchd.sh`（注册新 plist）
- `.env.example`（加 `ALLOWED_CHAT_ID`）

### 依赖
- 无新增 Python 包（requests 已有）

### 配置
- `.env` 新增 `ALLOWED_CHAT_ID`（默认 = `TG_CHAT_ID`）

## 9. 验收标准

1. 发送 `600519` → 30–90s 内收到 fresh 分支卡片
2. 发送 `贵州茅台` → 同上
3. 发送 holdings.yaml 里的代码 → 收到 holding 分支卡片
4. 发送 `688xxx` 科创板代码 → 立即收到拒绝卡（无 CC 调用）
5. 发送 ST 票 → 立即收到拒绝卡
6. 发送 `你好` 等闲聊 → 无响应
7. 进程被 kill → launchd 自动拉起，期间消息不丢
8. 同时发 5 只票 → 前 4 只串行处理（1 跑 + 3 排队），第 5 只立即收到"忙"

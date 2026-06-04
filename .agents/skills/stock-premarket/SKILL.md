---
name: stock-premarket
description: A 股盘前交易计划生成器。题材轮动方向的短线决策辅助，输出今日是否出手、主攻/潜伏/备选/禁买决策单 + IM 推送。当用户在 09:30 开盘前要求"盘前"、"观察池"、"premarket"、"今日候选股"、"今天看什么"，或自然语言"A股短线 盘前分析"时调用此 skill。
---

## Codex automation 契约

本 skill 会被 Codex automation 无人值守触发。执行时必须产出下面列出的文件和推送副作用；不要只回复完成。如果任一步骤失败，必须说明具体失败步骤，并停止声称成功。

- 必须生成 fact pack，并只使用 allowed facts。
- 必须写入 `data/last_card.md`。
- 必须在卡片末尾包含 ```decision_tickets fenced JSON block，并运行 `record_decisions.py --file data/last_card.md` 落库。
- 必须通过 `.agents/skills/stock-premarket/scripts/push.py` 推送。
- 非交易日、数据源失败、解禁检查失败、IM 推送失败时必须报告具体原因。
- 输出层级：文件内容是完整卡片，IM 推送是完整卡片，Codex automation 最终回复只给简要运行摘要。

# 定位说明

这是「**交易决策漏斗**」而不是资讯观察池。盘前必须把信息压缩成：今天做不做、主攻哪一只、潜伏/趋势试错是否允许、哪些票明确不买。盘中 stock-intraday 和 watch_loop 以这张决策单为基础；theme_emergence_loop 的自动题材候选只写 `decision_tickets(lane=trend)`，`watchlist_dynamic` 仅供手工 `/watch`。

输出**不是**「今天有什么事」，而是「**今天最多做什么、什么条件不满足就空仓**」。

# 工作流

每次调用按序执行下面 5 步，不要跳。

# 输出契约（最重要，违反 = 整体失败）

**所有数据必须有来源支撑**（[[feedback-data-must-be-sourced]]）。

`fetch_data.py` stdout 末尾输出 `=== ALLOWED === { ... } === /ALLOWED ===` JSON，是**该次卡片唯一允许引用的事实清单**：

- 6 位股票代码 → 必须在 `ALLOWED.codes` keys
- 中文股票名（stock_basic 5200 条匹配）→ 必须在 `ALLOWED.codes` values
- "N 板/N 连板" → 必须等于 `ALLOWED.lianban[code]`
- "±X.X%" → 必须在 `ALLOWED.pct[code] ± 0.5%`
- "涨停 N 只 / 炸板 N 只" → 精确等于 `ALLOWED.summary.{limit_up, broken}`
- "最高 N 连板" → 等于 `ALLOWED.summary.max_consec`
- 隔夜消息条目 → 必须在 `ALLOWED.news` 里有 title 相似度 ≥0.55 的条目

**禁止**：从训练数据印象/holdings.example.yaml 编出 fact pack 之外的股票。
**禁止**：编造连板数、数值偏差超过 ±0.5%、虚构隔夜新闻。

推送脚本 `push.py` 自动跑校验器（warn 模式留审计日志；enforce 模式拒推）。
**写卡前自己对照 ALLOWED 一遍**。

写入文件和 IM 推送的内容必须是卡片本身；Codex automation 最终回复只给简要运行摘要，不要用“完成”替代文件写入和推送。

## Step 1 · 拉确定性数据

```bash
.venv/bin/python .agents/skills/stock-premarket/scripts/fetch_data.py
```

读完整输出。这是 fact pack，**七节**：
1. 涨停结构（含连板个股清单）
2. 热门行业 Top 5
3. 炸板池
4. 龙虎榜净买入 Top 5（**含上榜原因**，例如「连续三日累计涨幅 20%」）
5. **同花顺热点 · 题材归因（D-1 盘后数据）** — 含 5.1 题材标签 Top 8 聚合表 + 5.2 强势股个股 reason
6. 近 10 日情绪指标
7. **隔夜消息面（D-1 15:00 → 现在）** — 7.1 命中题材消息（CLS+EM 合并、按时间倒序、带 URL）+ 7.2 题材命中频次 + 7.3 未命中关键词的消息

**所有价格、家数、连板数、净买入金额必须直接引用 fact pack 数据，禁止虚构或重算。**

## Step 1.5 · 读最近一份周复盘（先验种子）

继承 L7 stock-weekly 的下周方向，作为今日观察池的先验：

```bash
ls -1 data/weekly_review/*.md 2>/dev/null | sort | tail -1
```

若有结果：用 Python 解析其 machine-readable YAML 块：

```python
from pathlib import Path
from stock_codex.market.weekly_pack import parse_machine_readable
import glob
files = sorted(glob.glob("data/weekly_review/*.md"))
parsed = parse_machine_readable(Path(files[-1])) if files else None
```

`parsed` 结构（若非 None）：
- `week` ISO 标签
- `sentiment_stage` 上周末判定
- `themes[]` 每条含 `name / stance / leaders / catalysts / risks / match_score`
- `discipline_notes` 操作纪律提醒
- `web_status` ok / degraded

**如何使用**：

1. 把 `themes[].leaders` 作为今日观察池的 **先验种子**（候选股池）
2. 每只 `themes[].leaders` 的股票若被纳入今日观察池，标注来源 `（来自周复盘 W{week}）`
3. 今日 fact pack 的盘前异动股若与 `themes[].leaders` 交叉，标注 `（周复盘 + 今日异动 双重信号）`
4. 若 `parsed["themes"][i].catalysts` 里有今日日期的催化事件，在卡片头部 banner 提示
5. 若发现 L7 某主线已被周内消息证伪（例如周一开盘前的重大反向消息），在卡片末尾追加 `⚠ 周复盘主题「X」可能已破位，原因：...`

**降级**：`files == []` 或 `parsed is None` → 跳过此 Step，正常走原流程，不报错。

继承 [[feedback-news-awareness]]：先验种子只是候选，最终拍板必须叠加今日盘前消息。

## Step 2 · 抓今日新闻面（24h 窗口，叠加 fact pack 第七节）

fact pack 第七节已经把 CLS+EM 财经直播抓完（200+ 条、带 URL、带题材标签）。Step 2 在此基础上**补充结构化检索**：

依次抓，每个源失败就跳，不阻塞：

1. fact pack 第七节已覆盖 CLS+EM 财经直播 → **直接引用，不重复 WebFetch**
2. **WebSearch** 限定 2026 年最近 2 天（fact pack 第七节没覆盖的政策/公告类）：
   - "证监会 公告"
   - "央行 货币政策"
   - "工信部 OR 发改委 政策"
3. **WebSearch** fact pack 第 7.2 节命中频次 Top 3 主题的产业链深度消息
4. **WebSearch** 夜盘海外联动：美股纳指 / 中概 ADR / 原油 / 黄金（fact pack 7.1 有部分但要补宏观）

**约束**：每条新闻必须带 URL + 发布日期；24h 内；禁用淘股吧/股吧/微博/雪球热议；抓不到明确写"无显著消息面"，不要编。

**重要**：fact pack 第 7.3 节"未命中关键词的消息"必须扫一遍 —— 关键词表是固定的，新主线（如未在表里的题材）会落到这一节，是新方向冒头的早期信号。

**新闻判定**（继承 [[feedback-news-awareness]]）：每条新闻必须标注对今日观察池/持仓的影响：
- **题材驱动**：命中 fact pack reason tag Top 3，加权进重点题材
- **持仓利空**：直接命中 holdings.yaml 任一持仓股的负面新闻 → 卡片单列"开盘特别提示"
- **新主线候选**：尚未在 fact pack 出现的政策/产业事件 → 不进观察池但 ⚠️ 标"留待今日盘中观察"
- **无关**：背景信息，不进卡片

题材轮动派的本质是消息驱动，不看消息等于瞎打。

**2.x · 读昨日 L3 anomaly findings**（若存在）

```bash
ls data/anomaly_findings/ 2>/dev/null | tail -1
```

取**最近一份** `data/anomaly_findings/YYYYMMDD.md`（通常是前一交易日）读进来。来源是 L3 stock-anomaly 盘中调用产生的"冒头新方向候选"清单。

**用法约束**：
- 仅作"昨日尾盘异动出了啥题材"的背景输入
- **不**自动加进今日观察池
- 仅在 Step 3a/3b 题材判断时允许引用："昨日 L3 标记了 XX 题材冒头，今日 fact pack/新闻是否验证"
- 文件不存在 → 跳过，不影响主流程

## Step 3 · 综合判定（按下面 3a/3b/3c 顺序做）

### 3a. 情绪周期阶段判定

对照阈值（按当日 fact pack 数字判断，**不依赖历史数据也要给出判定**）：

| 阶段 | 涨停家数 | 炸板率 | 连板结构 | 其他 |
|------|---------|--------|---------|------|
| **退潮** | < 30 | > 50%（炸板/涨停） | 高度梯队消失 | 跌停 > 30 |
| **高位分歧** | 30-60 | 30-50% | 高度股断板 | 跌停增多 |
| **启动** | 30-60 | < 30% | 2-3 板出现 | - |
| **加速** | > 60 | < 30% | 5 板+ 出现 | 主线明确 |
| **一致** | - | - | 分歧次日龙头放量回封 | - |

⚠️ **关键规则**：

1. **当 fact pack 第五节"近 10 日情绪指标"为空时**，**仍然要判定**，但句末标"（基于单日特征，无历史对比验证）"。
2. **绝不能用"数据积累中"作为回避判断的借口**。当日数据已经明确指向某阶段时，必须给出该阶段。
3. **涨停 < 30 是硬退潮信号**——无论历史数据如何，必须标"退潮"或"退潮风险"。这种情况下观察池**必须明确建议空仓或仅低吸超跌**，禁止推接力。
4. **涨停从前一交易日 > 50 骤降到 < 20**（剧烈缩量）也是退潮信号，即使绝对数字不算极低。这条优先级最高。
5. 跨天剧烈变化（如前日 57 今日 10）即使无 10 日基线，也必须明确判定阶段切换。

**阶段判定与观察池建议的强联动**：
- 退潮：观察池 ≤ 2 只（仅低吸超跌或空仓），明显标"建议空仓"
- 高位分歧：观察池 3-5 只（高低切方向）
- 启动：观察池 5-8 只（接力 + 低吸混合）
- 加速：观察池 5-8 只（接力为主）
- 一致：观察池 ≤ 3 只（最后一棒接力，慎重）

### 3b. 重点题材 Top 3

候选维度（综合判断，优先级从高到低）：

1. **同花顺 reason tag 高频题材**（fact pack 第 5.1 节）：出现次数 ≥ 3 的题材自动入选 Top 3 候选池。这是机器可读的题材维度，比按行业 Top 5 粗聚合更准——例如「算电协同」「人形机器人」可能跨多个行业出现。
2. fact pack 行业 Top 5（连板高度 > 涨停股数）。
3. 龙虎榜上榜原因里反复出现的题材关键词（fact pack 第 4 节）。
4. 新闻面有明确政策 / 产业链驱动的，加权上调。

**矛盾时**：reason tag 题材 ∩ 新闻面驱动 ∩ 龙虎榜资金 = 最强；只有一项命中 = 弱信号。

### 3c. 决策漏斗：主攻 / 潜伏 / 备选 / 禁买

候选优先级：
1. fact pack 最高连板个股（情绪锚）
2. 重点题材的龙头股
3. 龙虎榜净买入 Top 5 与题材吻合的票
4. 强势股池里二板候选
5. **fact pack 第 5.2 节强势股清单中 reason tag 与今日 Top 3 题材匹配的票**（同花顺人工标注，质量高）

先按上面候选优先级收集候选，再压缩成五个 lane：

| lane | 数量 | 含义 | 输出动作 |
|------|------|------|----------|
| `main` | 0-1 | 今日主攻，只能有一只 | `buy_if`，给买入区间、最多追价、截止时间 |
| `ambush` | 0-2 | 派别 E，消息强但盘面未启动 | `buy_if`，只低吸，不追高，给等待期 |
| `backup` | 0-2 | 主攻作废后才看 | `wait`，若进入 `decision_tickets` 必须给完整触发区间；否则只写在人读段 |
| `trend` | 0-2 | 短线趋势试错，题材强度/相对强度/放量突破共振 | `buy_if`，小仓，不打板，给趋势买点、追价上限、止损 |
| `ban` | 不限 | 明确不买 | `avoid`，写清禁买理由 |

**硬约束（违反即视为输出残次）**：

- **每只候选必须有具体代码（6 位）+ 名称 + 行业**。不允许只写"方向""跟风方向""低位补涨方向"之类的占位。
- 候选**必须来自 fact pack**（涨停股清单 / 连板清单 / 龙虎榜 / 同花顺强势股 / 热门行业 Top 5 的成分股）。新闻 / WebSearch 提到的题材如果在 fact pack 里找不到对应的具体票，**不要为它创造观察池位置**，宁可观察池数量少一只。
- 例如：fact pack 行业 Top 5 是电力/电网/通用设备/汽车零部/房地产，新闻提到"算力"但 fact pack 里没有算力相关的涨停股 → **算力题材不进观察池**，可以在"重点题材"部分提一句"算力题材有新闻驱动但今日无强势个股，留待后续观察"。
- **解禁过滤（30 天硬约束）**：每只候选生成后**必须**执行：
  ```bash
  .venv/bin/python .agents/skills/stock-premarket/scripts/extras.py --lockup <6位代码>
  ```
  检查 `upcoming` 数组。如果未来 30 天内有任何一条 `float_ratio > 0.05`（占流通市值 > 5%），**剔除该候选**或在备注里红色标注。少一只无所谓，不留雷。
- 决策单必须收敛。有效候选很多时也只能给 1 个主攻；没有好机会时 `main` 可以为空，但若是修复/启动日且趋势条件成立，可以给 1-2 只 `trend` 小仓试错，不再把“无主攻”自动等同于全天空仓。
- `decision_tickets` 是可执行机器单：`main` / `ambush` 必须包含 `entry_low`、`entry_high`、`stop_price`、`deadline_time`、`size_pct`；`main` 还必须包含 `max_chase_price`。
- `backup` 若进入 `decision_tickets`，必须同时包含 `entry_low`、`entry_high`、`max_chase_price`、`stop_price`、`deadline_time`、`size_pct`；没有这些字段的备选只能写在人读段，不得落库。
- `trend` 若进入 `decision_tickets`，必须同时包含 `entry_low`、`entry_high`、`max_chase_price`、`stop_price`、`deadline_time`、`size_pct`；默认单票 10-15%，只做趋势突破/回踩，不追涨停板。
- 如果没有完整可执行买点，今日总决策必须写“今日无可下单信号”，不要把半成品 backup 塞进机器块。

**每只可交易候选必须按下面 5 个派别之一标记**：

#### 派别 A · 二板接力（最可代码化）
- **适用**：昨日首板今日预期高开承接 / 已经 1 板今日打 2 板的票
- **买点**：昨日封板价 × 1.01（盘前可定死价位）
- **止盈**：次日竞价 +3%~+7% 出，或破日内分时均线
- **止损**：跌破开盘价 / 跌破昨日封板价（约 -3%~-5% 硬止损）
- **仓位**：首仓 ≤30%（当前可用 X%）

#### 派别 B · 龙头补涨 / 低吸
- **适用**：题材龙头近期回踩 5 日线、缩量充分的票
- **买点**：5 日线 ± 1% + 缩量 30% 以上
- **止盈**：+5%~+8% 减半仓；破 5 日线 2 日未收回清仓
- **止损**：跌破 5 日线 -3% 硬止损
- **仓位**：首仓 ≤30%（当前可用 X%）

#### 派别 C · 超跌反弹 / 低吸
- **适用**：连续阴跌后底部 K 线确认 + 量比 > 2 的票
- **买点**：前低 + 1%，或日内分时低点
- **止盈**：反弹至前压力位减仓，T+1 不强势就出
- **止损**：-3% 硬止损
- **仓位**：首仓 ≤20%（当前可用 X%）

#### 派别 D · 首板候选（盘前**不**给硬价）
- **适用**：题材发酵期可能首次涨停的票
- **买点**：盘前不定价。**必须盘中人工判断**封板时机
- **止盈**：次日竞价 +3%~+7% 或开板回封
- **止损**：跌破封板价立即出；当日炸板 10 分钟内不回封 → 走
- **仓位**：首仓 ≤20%（当前可用 X%）
- ⚠️ 此派别**只标候选 + 触发条件**，不给具体买入价格。LLM 不要为这类票虚构买点。

#### 派别 E · 事件预期埋伏（只进 `ambush`）
- **适用**：消息面/政策/产业催化明确，但盘面还未大面积启动，标的处在相对低位。
- **前提**：催化最好在 1-10 个交易日内；受益逻辑必须具体到主营、订单、产能、客户、牌照或地域，不接受"可能沾边"。
- **买点**：只给低吸区间 `entry_low-entry_high`，不允许追高；若当日已涨 5% 以上，通常不再算埋伏。
- **仓位**：首仓 10%-15%，最高不超过 20%（当前可用 X%）。
- **失效**：催化落地无反应、题材被证伪、跌破关键低位/20 日线、超过等待期未启动。
- **升级**：板块出现 3 只以上涨停 + 个股放量突破，才允许从潜伏观察升级为后续主攻候选；当天不要临时追高。

#### 派别哲学声明（必须包含在输出里）
本系统走**赵老哥派**严格止损路线，**不**沿用炒股养家"永不止损"。任何输出不得出现"长期持有""逢低补仓死扛"等违反纪律的措辞。

#### 机器单止盈目标（`target_pct`）
- 只在存在明确数字化第一止盈目标时写入 `decision_tickets.target_pct`，单位为百分比，不写计划止盈价格。
- 派别 A / D 默认取纪律区间下沿 `3`；派别 B 默认取下沿 `5`。
- 派别 C / E 若没有清晰数字目标，允许留空，禁止为了字段完整虚构止盈。
- `/buy` 会按真实成交价计算持仓 `take_profit`，因此 `target_pct` 必须相对成交成本表达。

## Step 3.5 风控预检（必做，不可跳过）

执行命令获取风控 JSON：

```bash
.venv/bin/python .agents/skills/stock-premarket/scripts/preflight.py
```

返回字段：
- `banner` (string|null) — 超额提示文案。非 null 时**必须**作为卡片正文第一行，紧接一行分隔符 `———————————————————————`。
- `available_pct` (number) — 剩余可用仓位 %。**每只候选股的"仓位"行**必须改写为：
  - 原文：`仓位：首仓 ≤30%`
  - 新文：`仓位：首仓 ≤30%（当前可用 X%）`，其中 X 取 `available_pct`，整数显示。
- `holdings` (array) — 当前旧持仓明细，含成本、止损、止盈和原交易假设。非空时必须先逐只给出处理动作，再讨论新买入。
- `exposure_pct` / `position_count` — 仅日志参考。

兜底：风险计算失败时返回保守默认（banner=null, available_pct=30），但会尽量保留已读取的 `holdings`；不得因风险模块异常把旧持仓写成空仓。

## Step 4 · 输出 IM 卡片

模板严格按下面格式（替换 [...] 占位）：

（若 preflight.banner 非空，将其作为卡片首行；X 由 preflight.available_pct 整数填入）

````
📋 盘前交易计划 · [YYYY-MM-DD]

🌡️ 情绪阶段：[启动/加速/分歧/一致/退潮]
   [一句话理由，引用 fact pack 数字]

📌 旧持仓优先处理
[preflight.holdings 为空时写"无旧持仓"]
1. [代码] [名称] · 成本 [cost] · [派别 genre]
   动作：[持有观察 / 竞价减仓 / 触发止损卖出 / 到止盈位分批兑现]
   纪律：止损 [stop_loss 或"未设置，开盘前补齐"]；止盈 [take_profit 或"未设置，结合原交易假设处理"]
   依据：[note；为空时写"原交易假设缺失，禁止无依据加仓"]

🎯 今日总决策：[空仓 / 只做主攻 / 主攻+潜伏]
- 结论：[30 秒内能执行的一句话]
- 可下单信号：[有 N 条 / 今日无可下单信号]
- 仓位上限：[总仓位 X%，单票 X%]
- 今日最重要作废条件：[如 10:30 前主攻不触发则空仓]

🔥 今日重点题材
1. [行业] · [N 只涨停] · [最高 N 板 + 龙头代码 名称]
   驱动：[新闻 URL + 日期 / "延续昨日"]
2. ...
3. ...

✅ 主攻（0-1 只）
[没有主攻时写"无，今日主线/情绪不支持出手"]
1. [代码] [名称] · [派别 A/B/C/D] · [题材]
   动作：只在 [条件] 满足时买
   买入区间：[entry_low-entry_high]；最多追价：[max_chase_price]
   止损：[stop_price]；仓位：[size_pct]%
   截止：[deadline_time]；作废：[invalid_conditions]

🟡 潜伏（0-2 只，派别 E）
1. [代码] [名称] · [题材]
   动作：只低吸，不追高
   低吸区间：[entry_low-entry_high]；仓位：10-15%
   等待期：[deadline_time]；失效：[invalid_conditions]
   升级条件：[upgrade_conditions]

⏳ 备选（0-2 只）
- [代码] [名称]：只有主攻作废后才看；触发条件 [若无完整买点，明确写“仅观察，不进入机器单”]

🚫 禁买
- [代码 名称]：[位置高 / 高潮 / 沾边不纯 / 无盘面确认 / 解禁风险]
- [题材级禁买] 只写在人读段，不进入 `decision_tickets`；进入 JSON 的 `ban` 必须有 6 位代码和名称。

📰 重点消息（[N] 条）
- [日期] [标题] → [题材驱动 XX / 持仓利空 XX / 新主线候选]
  [URL]
（无显著消息面写"24h 内无显著消息，关注开盘竞价"）

⚠️ 风险提示
- [一两条基于炸板池/连板高度/退潮信号]
- 提醒：9:50 前未触发的票今日放弃，不追高
- 提醒：止损必须双轨——同花顺条件单 + Codex 推送

---
本系统纪律：赵老哥派严格止损 · 单只首仓 ≤30% · 总仓位看情绪阶段
fact pack: data/fact_pack/[YYYYMMDD]_premarket.md

```decision_tickets
{
  "trade_date": "YYYY-MM-DD",
  "tickets": [
    {
      "code": "000000",
      "name": "示例股份",
      "concept": "示例题材",
      "lane": "main",
      "faction": "A",
      "action": "buy_if",
      "entry_low": 10.0,
      "entry_high": 10.3,
      "max_chase_price": 10.4,
      "stop_price": 9.7,
      "target_pct": 3,
      "invalid_price": 9.6,
      "deadline_time": "10:30",
      "size_pct": 20,
      "thesis": "一句话交易假设",
      "evidence": {"source": "fact pack"},
      "invalid_conditions": ["10:30 前不突破则作废"],
      "upgrade_conditions": []
    }
  ]
}
```
````

**落库 + 推送**（三步，避免 shell escape 问题）：

1. 用 Write 工具把上面整段 Markdown 写入 `data/last_card.md`
2. 先落库决策单：
   ```bash
   .venv/bin/python .agents/skills/stock-premarket/scripts/record_decisions.py --file data/last_card.md
   ```
3. 再推送；`push.py` 会剥离 `decision_tickets` 机器块，不把 JSON 发到 IM：
   ```bash
   .venv/bin/python .agents/skills/stock-premarket/scripts/push.py --file data/last_card.md
   ```

## Step 5 · 终端简要返回

返回 2-3 句：
- 阶段判定结论
- 决策单数量 + lane 分布（如 "1 主攻 / 1 潜伏 / 2 禁买"）
- 推送状态（msg_id）

不要重复打印整个卡片。

---

# 信息源白名单

| 源 | URL | 用途 |
|----|-----|------|
| 财联社早盘速递 | https://www.cls.cn/depth/1003 | 24h 滚动 |
| 财联社电报 | https://www.cls.cn/telegraph | 头条 |
| 证监会 | csrc.gov.cn | 政策原文 |
| 上交所 | sse.com.cn | 公告 |
| 深交所 | szse.cn | 公告 |
| 新华财经 | news.cn/fortune | 权威媒体 |
| 第一财经 | yicai.com | 产业链分析 |

**禁用源**：淘股吧、东方财富股吧、微博、抖音、知乎、雪球热议、各类"VIP 内幕群"截图。

# 强约束清单

1. fact pack 数字直接引用，不重算
2. 新闻必须带 URL + 发布日期；24h 内
3. 不用情绪化词："暴涨""狂飙""错过""必涨"
4. 不出 "buy"/"sell" 命令，只给"观察理由 + 派别 + 买卖纪律"
5. **首板候选派只标候选，不虚构具体买入价**（但仍必须给具体代码 + 名称，不允许只写"方向"）
6. 输出卡片 ≤ 2000 字
7. 任何步骤失败明确写"无数据"，不要编
8. 必须包含「9:50 后未触发放弃」和「双轨止损」两条风险提示
9. **观察池每只候选必须查 30 天内解禁**（调 `extras.py --lockup`），`float_ratio > 5%` 的不进观察池
10. **重点题材必须优先采纳同花顺 reason tag 高频项**（fact pack 第 5.1 节），不能纯凭新闻面臆测
11. **新闻必须标注命中维度**（[[feedback-news-awareness]]）：题材驱动 / 持仓利空 / 新主线候选 / 无关。无标注的消息条目视为输出残次。持仓利空必须单列"开盘特别提示"。

# 触发示例

- "用 stock-premarket skill"
- "生成今日盘前观察池"
- "盘前看看"
- "premarket"
- "今天看什么"

# 升级路径（不必现在做）

- fact pack 升级到同花顺概念维度
- LLM 看到重要题材时调 `.venv/bin/python .agents/skills/stock-premarket/scripts/fetch_data.py --concept "电力"` 拉成分股详情
- 接入 09:15-09:25 竞价数据 → stock-bidding skill 做第二次收敛推送
- stock-intraday skill 盯盘到价告警
- stock-postmarket skill 复盘 + 次日题材延续判断

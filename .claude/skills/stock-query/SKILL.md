---
name: stock-query
description: 单股深度分析。给定一个 A 股代码（主板/创业板），按题材派框架判定值不值得买/继续持有，给买点/止损/止盈。当用户传入参数 code=XXXXXX mode=fresh|holding 时触发。
---

# stock-query · 单股决策助手

**调用方**：`scripts/tg_listener.py` 通过 `claude -p` headless 触发。
**入参**：在 prompt 文本中以 `code=600519 mode=fresh` 形式传入。

## 工作流（按序，不跳）

### Step 1 · 拉数据 fact pack

```python
import sys
sys.path.insert(0, "code")
from lib import query

CODE = "<从入参解析>"
realtime = query.fetch_realtime(CODE)            # 必拿，失败直接报错回避
kline    = query.fetch_kline(CODE, days=60)      # 60 日日线
flow     = query.fetch_money_flow(CODE, days=5)  # 资金流
concept  = query.fetch_concept_strength(CODE)    # 概念榜
news     = query.fetch_recent_news(CODE, days=7) # 新闻
```

任一**非 realtime** 拉取失败 → 标"该项数据缺失"，结论档位降一档（买入→观察，观察→回避）。

mode=holding 时额外读 `holdings.yaml` 当前票的成本价、仓位、buy_date、stop_loss。

### Step 2 · 题材派六维判定

| 维度 | 怎么判 |
|---|---|
| 题材归属 | 用 fact pack 概念榜 Top 20 + 近 10 日 ths_hot_reason 表（DB 已有）反查该票主题材；找不到归类→标"无明确主线" |
| 题材位置 | 启动期：概念近 5 日累涨 5–15%、龙头刚加速；主升期：概念 5 日 >15% 且涨停股 ≥3；高潮期：龙头连板 ≥4 或概念单日 >5%；退潮期：概念近 3 日累跌或龙头炸板 |
| 个股位置 | 比对概念龙头：相对涨幅 vs 龙头近 5 日差值 → 龙头/二线/边缘 |
| 资金 | 近 3 日 main_in 累计正负、单日峰值 |
| 技术 | 收盘 vs MA5/MA10/MA20；近 20 日相对高低位置；量比 |
| 消息 | news 列表前 5 条标题，标"利好/利空/中性"，是否有公司公告突发 |

### Step 3 · 结论档位

**fresh 分支**：
- **买入**：题材在启动/主升 + 个股是龙头或紧跟龙头 + 资金净流入 + 技术不在高位（距 20 日高 ≥5%）
- **观察**：方向对但任一维度不达标。**必须列出"什么信号出现升级为买入"**（≥2 条具体可观测信号）
- **回避**：题材退潮 / 高位滞涨 / 资金持续 3 日净流出 / 技术破位 之一即触发

**holding 分支**：
- **加仓**：买入逻辑仍然成立且未到第一止盈
- **持有**：维持原计划；同步更新止损是否该上移（盈利 >5% 时止损上移到成本价）
- **减仓清仓**：原买入逻辑失效（题材退潮、资金转流出、跌破关键位）

### Step 4 · 关键价位（每档都必给）

- 买点：限价 或 触发条件（含价位）
- 止损位：具体数字 + 依据（前低 / MA20 破位）
- 止盈位：第一目标 / 第二目标（按概念龙头近期高点 + 个股压力位）

### Step 5 · 输出卡片

**严格按下面模板输出（替换花括号占位符）。不要加额外段落、不要解释思考过程。**

fresh 模板：

```
📊 {NAME} {CODE}  [买入 / 观察 / 回避]
━━━━━━━━━━━━━━━━
🎯 结论：{VERDICT}
理由：{1-2 句白话}

🏷 题材：{CONCEPT} · {PHASE} · 板块5日{X}%
📍 位置：{LEADER_OR_FOLLOWER}（龙头{LEADER_NAME}，相对{Y}%）
💰 资金：近3日主力净{IN_OR_OUT}{Z}亿
📈 技术：{MA_POSITION}
📰 消息：{NEWS_SUMMARY}

⚡ 升级为"买入"的信号（满足任一）：   ← 仅观察档输出
  · {SIGNAL_1}
  · {SIGNAL_2}

💵 关键价位
  买点：{BUY_TRIGGER}
  止损：{STOP_LOSS}（{REASON}）
  止盈：{TP1} / {TP2}
━━━━━━━━━━━━━━━━
⚠️ 短线纪律：观察档≠可建仓，等信号    ← 仅观察档
```

holding 模板：

```
📊 {NAME} {CODE}  [加仓 / 持有 / 减仓清仓]
━━━━━━━━━━━━━━━━
🎯 结论：{VERDICT}
持仓：{COST}成本 · {DAYS}日前买入 · 当前{PRICE}（{PNL_PCT}）

🏷 题材：{CONCEPT} · {PHASE}
💰 资金：{FLOW}
📈 技术：{TECH}

⚠️ 触发{VERDICT}的逻辑：
  · {REASON_1}
  · {REASON_2}
  · {REASON_3}

🛡 {STOP_PLAN}
🎯 {TARGET_PLAN}
━━━━━━━━━━━━━━━━
```

### 限制
- 卡片单条 ≤ 800 字
- 价位必须给数字（非区间），止盈最多两档
- 任何数据缺失项标"—"不要编

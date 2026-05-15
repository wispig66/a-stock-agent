---
name: stock-ask
description: A股随时分析入口。给定一段自由文本（板块名/个股代码/事件文本/模糊描述），输出"值不值得参与 + 推荐标的 + 买点"卡片。当用户传入参数 text="..." mode=normal|deep 时触发。
---

# stock-ask · 随时分析

**调用方**：`scripts/tg_listener.py` 通过 `claude -p` headless 触发。
**入参**：prompt 文本中以 `text="..." mode=normal` 形式传入。

## Step 1 · 意图分类

```python
import sys
sys.path.insert(0, "code")
from lib import intent, sector_pack, event_pack

TEXT = "<从入参解析>"
MODE = "<normal|deep>"

lex = sector_pack._load_lexicon()
# 规则全不命中时才掉到 LLM 桥（这里 llm_call 用真 Claude 通过 build_llm_prompt 走子查询）
r = intent.classify(TEXT, lexicon=lex, llm_call=None)  # 先纯规则；下面再 LLM 兜底
```

如果 `r["intent"] == "ambiguous"` 且规则未命中：调用 LLM 跑一次 `intent.build_llm_prompt(TEXT)`，解析 JSON 重组成 `{intent, extracted, confidence}` 再走分支。
LLM 也判模糊 → 输出"模糊兜底卡片"（见 Step 4d），不要瞎猜。

## Step 2 · 路由

| 意图 | 行动 |
|---|---|
| `stock`   | 用 `Skill` 工具调用 `stock-query`，参数 `code=<extracted> mode=fresh`，**直接退出本 skill**，把 stock-query 输出原样回 TG |
| `sector`  | `pack = sector_pack.build_sector_pack(extracted)` → Step 3 板块卡 |
| `event`   | 跑 `event_pack.build_event_pack(...)` → Step 3 事件卡 |
| `ambiguous` | Step 4d 兜底卡片 |
| `error`   | `❌ <extracted>` 一行错误回 TG |

## Step 3 · 数据采集（板块/事件分支）

**板块**：`pack = sector_pack.build_sector_pack(extracted)`。`pack["verdict_modifiers"]` 非空时结论降一档。

**事件**：
```python
def categorize(text):
    # 用 LLM 跑一次，prompt 见下方 EVENT_PROMPT
    ...
def web_fetch(q, timeout):
    # 仅 deep 模式；调 web-access skill
    ...
pack = event_pack.build_event_pack(
    TEXT, mode=MODE, categorize=categorize,
    sector_pack_fn=sector_pack.build_sector_pack,
    web_fetch=web_fetch if MODE == "deep" else None,
)
```

EVENT_PROMPT（喂给 LLM）：
> 用户报告了一个 A 股相关事件。请判断：
> 1. 事件类型（政策/产品发布/订单中标/突发利好/突发利空/其他）
> 2. 可能受益的板块（A 股投资者真实用的题材分类，不要用 "新能源行业"，用 "光伏" "储能" "氢能" 这种）
> 3. 可能受损的板块
> 4. 1 句话传导逻辑
>
> 事件文本：{event_text}
> 严格输出 JSON：{"event_type":"...","candidate_sectors":["...","..."],"risk_sectors":["..."],"core_logic":"..."}

## Step 4 · 输出卡片

### 4a 板块卡

```
🎯 {SECTOR}  [参与 / 观察 / 回避]
━━━━━━━━━━━━━━━━
📊 结论：{1 句白话}

🏷 阶段：{STAGE}
   板块今日 {+X%} · 近 5 日 {+Y%} · 涨停股 {N} 只

📰 消息面：
   · {新闻1}（{利好/中性/利空}）
   · {新闻2}
   核心驱动：{1 句}

💰 资金 + 情绪：
   龙头：{NAME} {CODE}（{N}连板，今日{+X%}）
   板块成交额：{X}亿 / 5日均 {Y}亿（{放量/缩量}）

📈 技术：
   龙头位置：{MA 描述} · 距 20 日高 {Z}%
   板块指数 60 日 {高位/中位/低位}

🎯 推荐标的（Top 3-5）：
   1. {NAME1} {CODE1} · {龙头/二线} · 买点 {价位} / 止损 {价位}
   2. ...

⚡ 升级"参与"的信号（仅观察档）：
   · {SIGNAL_1}

⚠️ 风险：{1-2 句}
```

Top 3-5 不足 3 只时只列实际数量 + 标"候选不足"。
`verdict_modifiers` 非空时结论档位降一档（参与→观察，观察→回避）。

### 4b 事件卡

```
⚡ 事件解读  [机会 / 中性 / 风险]
━━━━━━━━━━━━━━━━
📰 事件：{event_text 摘要}
   类型：{event_type}
   核心逻辑：{core_logic}

🎯 受益板块：
   1. {SECTOR1} ✓ 题材库验证 · 阶段 {...}
      龙头：{NAME} {CODE}（{N}连板）
   2. {SECTOR2} △ 近似匹配 · 阶段 ...
   3. {SECTOR3} 🌐 实时验证 ...     ← 仅 deep 模式

⚠️ 风险板块：{...}（仅提示，散户不做空）

🎯 直接推荐标的：
   1. {NAME1} {CODE1} · 买点 {价位} / 止损 {价位}
      理由：{所属板块 + 在板块中位置}

💡 操作建议：
   {1-2 句}

⚠️ 风险提示：
   · {若 degraded=True 加"市场尚未确认该方向"}
   · 事件型机会通常 1-3 天兑现完毕

──────────────
模式：{normal | deep ✓} {web_status=timeout 时加 ⚠️ 实时搜索超时}
```

### 4c 模糊兜底

```
🤔 没听清你想问哪个，可能是：
  A. 板块「{cand A}」
  B. 个股「{cand B}」
  C. 事件「{cand C}」
回复 A/B/C 或重新发 /ask sector=xxx
```

不做多轮交互——TG listener 无会话状态。

### 4d 错误

```
❌ {error_msg}
```

## 输出规范

- 仅输出卡片，不要解释思考过程
- 数据缺失项不要瞎编，写"—"或省略行
- 严格使用上面模板格式（不要 markdown 表格，TG 不渲染）

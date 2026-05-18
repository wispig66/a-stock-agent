---
name: stock-ask
description: A股随时分析入口。给定一段自由文本（板块名/个股代码/事件文本/模糊描述），输出"值不值得参与 + 推荐标的 + 买点"卡片。当用户传入参数 text="..." mode=normal|deep 时触发。
---

# stock-ask · 随时分析

**调用方**：`scripts/tg_listener.py` 通过 `claude -p` headless 触发。
**入参**：prompt 文本中以 `text="..." mode=normal` 形式传入。

# 工作流（按序，不跳）

> ⚠ 下面 3 个 lib 模块（`intent` / `sector_pack` / `event_pack`）已存在且可用。
> **直接 import 调用即可，不要先 Explore/Grep/Read 任何 lib 源码或 DB schema**。
> 区分两种"额外工作"：
> - ❌ **禁止修自己的 bug**：ImportError 不要换 import 路径、不要探 DB schema、不要 patch lib 源码 → 立即走 4d 错误卡
> - ✅ **允许真去研究问题**：sector_pack/event_pack 查无结果 → 按 Step 2.5 降级链继续，**不要立即放弃**

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
| `ambiguous` | Step 4c 模糊兜底卡片 |
| `error`   | `❌ <extracted>` 一行错误回 TG |

**下游异常处理**：
- `sector` 分支 `build_sector_pack` 抛 `SectorNotFound` / 返回空 → **走 Step 2.5 降级链**
- `event`  分支 `build_event_pack` 抛异常 / 返回空 → **走 Step 2.5 降级链**
- `stock`  分支 stock-query 子调用报错 → 走 4d，error_msg = "无法分析该股票"
- 任一步骤超过 90 秒未出结果 → 立即用已有信息出降级卡（4c 含已搜到的新闻）

## Step 2.5 · 降级链（sector/event 查无结果时）

不要立即兜底——按下面 3 步逐级降级，命中任一步即出对应卡片。**总耗时硬上限 90 秒**（normal）/ 180 秒（deep），触顶用已有信息出 4c 降级卡。

### 2.5a · LLM 重映射到题材库已知名（必跑，~15s）

让 LLM 把用户的模糊词映射到 3 个题材库可能存在的近义题材名：

```python
remap_prompt = f"""用户问的 A 股题材："{TEXT}"。
请给出 3 个 A 股投资者实际使用的标准题材名（不要"新能源行业"这种泛称，要"光伏"、"储能"、"氢能"这种具体名）。
严格输出 JSON 数组：["题材1", "题材2", "题材3"]
"""
# 用 Claude 本体跑这个 prompt，解析 JSON
candidates = [...]

for name in candidates:
    try:
        pack = sector_pack.build_sector_pack(name)
        # 命中 → Step 3 板块卡，标题加 "（按"{TEXT}"映射到 {name}）"
        break
    except SectorNotFound:
        continue
```

### 2.5b · fallthrough 到 event_pack（命中即用，~30s）

2.5a 全失败 → 当事件处理：

```python
# categorize() 让 LLM 解析 TEXT 是个啥事件，返回 candidate_sectors
pack = event_pack.build_event_pack(
    TEXT, mode=MODE, categorize=categorize,
    sector_pack_fn=sector_pack.build_sector_pack,
    web_fetch=web_fetch,   # normal 也允许（限 1 次、15s 超时）；deep 允许多次
)
# pack 非空 → Step 3 事件卡
```

### 2.5c · 仍无结果 → 4c 降级卡（含已采集的信息）

```
🤔 "{TEXT}" 不是 A 股常见题材名，已搜：
  · LLM 映射尝试：{candidate1}/{candidate2}/{candidate3} — 题材库均未命中
  · 事件分类：{event_type}（若 categorize 有结果）
  · 近期新闻摘要 1-2 条（若 web_fetch 有结果）
建议：
  · /ask sector=xxx 直接指定题材
  · 或换个更明确的关键词
```

4c 不再只是"我不懂"，要把已搜到的所有线索都给用户看。

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

### 4c 模糊兜底（**含已采集线索**，不是空壳）

仅在 Step 2.5 三步降级链全部失败、或触发 90s 硬上限时出。**必须把 2.5a/2.5b 已搜到的所有线索都列出来，让用户能基于线索追问**。

```
🤔 "{TEXT}" 在 A 股题材库无直接匹配

已尝试：
  · LLM 重映射：{cand1} / {cand2} / {cand3} — 题材库均未命中
  · 事件分类：{event_type}（若 categorize 有结果，否则省略此行）
  · 近期新闻（若 web_fetch 有结果，否则省略）：
    - {新闻标题1}（{日期}）
    - {新闻标题2}

可能方向：
  A. {基于已采集线索的最合理猜测，1 行}
  B. {次合理猜测}

下一步建议：
  · /ask sector=xxx 直接指定题材
  · 或换个更明确的关键词（如具体公司、明确事件）
```

**写 4c 时严禁瞎编**：任何"可能方向"必须基于 2.5a/2.5b 已采集的真实信息。如果三步降级全无信息（极少见），用最简版：`❌ "{TEXT}" 题材库与新闻面均无匹配，建议换关键词`。

不做多轮交互——TG listener 无会话状态。

### 4d 错误

```
❌ {error_msg}
```

# 强约束清单

1. **禁止前置探索**：不要 Explore/Grep/Read 项目源码；按上面步骤直接 import 并调用
2. **禁止修自己的 bug**：ImportError / 字段不存在 → 立即走 4d 错误卡；不要换 import 路径、不要探 DB schema、不要 patch lib 源码
3. **允许真去研究问题**：sector/event 查无结果 → 必须走 Step 2.5 降级链（LLM 重映射 → event_pack → 含信息的 4c），不要直接放弃
4. **总耗时硬上限**：normal 90s / deep 180s；触顶用已有信息出 4c 降级卡（不是空壳）
5. 数据缺失项不要瞎编，写"—"或省略行
6. 仅输出卡片，不要解释思考过程
7. 严格使用上面模板格式（不要 markdown 表格，TG 不渲染）
8. 卡片单条 ≤ 800 字（对齐 stock-query）

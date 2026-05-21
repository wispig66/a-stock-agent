---
name: stock-ask
description: A股随时分析入口。给定一段自由文本（板块名/个股代码/事件文本/模糊描述），输出"值不值得参与 + 推荐标的 + 买点"卡片。当用户传入参数 text="..." mode=normal|deep 时触发。
---

# stock-ask · 随时分析

**调用方**：`scripts/tg_listener.py` 通过 `codex exec` headless 触发。
**入参**：prompt 文本中以 `text="..." mode=normal` 形式传入。

# 工作流（按序，不跳）

> ⚠ 整个流程只有 3 步：跑 pipeline 拿 fact pack → 综合判断路由 → 出卡片。
> 直接按下方步骤执行，**不要 Explore/Grep/Read 任何项目源码或 DB schema**。
> 代码层异常（ImportError / 字段不存在）→ 立即 4d 错误卡。
> 业务层查无结果（pipeline 各字段都为空）→ 4c 含已搜线索的兜底卡。

## Step 1 · 跑 pipeline 拿 fact pack（一个命令）

```bash
.venv/bin/python scripts/stock_ask_pipeline.py --text "<TEXT>" --mode <MODE>
```

**只用这条命令**。不要 `which uv` / `command -v uv` / 探路径——`.venv/bin/python` 在项目根目录下永远存在。失败 → 直接 4d 错误卡。

stdout 是 JSON。预期 3-10 秒返回。4 个字段并发跑完：

| 字段 | 含义 | 用法 |
|---|---|---|
| `lexicon.rule_intent` | 规则分类预判 `{stock, sector, event, ambiguous, error}` | 强 confidence 时直接信 |
| `lexicon.nearest_sectors` | 题材库 Top 3 最相似项 | 即便 rule_intent=ambiguous 也能拿到候选 |
| `lexicon.lexicon_size` | 题材库总条数 | 为 0 说明 DB 没数据，全部按 event 走 |
| `stock_match.matched` + `via` | 个股精确匹配（code / name_unique / name_ambiguous） | matched=True 时直接转 stock-query |
| `db_frequency.ths_hot_reason_hits` | 近 7 日命中条数 | >3 说明是近期热点 |
| `db_frequency.sample_reasons` | 命中的 reason 原文 | 用来定标准题材名（例 "算力租赁+Token工厂+通信网络管维" 里抽 "Token工厂"） |
| `local_news.news` | 近 24h 含该词的新闻 | 不为空说明是近期事件型话题 |

## Step 2 · 综合判断路由（**不要再调 LLM 分类**，直接看 fact_pack 想清楚）

按下面优先级（命中即用）：

| 判定 | 触发条件 | 行动 |
|---|---|---|
| **个股** | `stock_match.matched == True` 且 `via in ("code","name_unique")` | 用 Skill 工具调 `stock-query`，参数 `code=<code> mode=fresh`，**直接退出本 skill** |
| **个股歧义** | `stock_match.via == "name_ambiguous"` | 输出候选列表让用户重选（不调 stock-query） |
| **已知题材** | `lexicon.rule_intent == "sector"` 或 `nearest_sectors[0]` 在 `sample_reasons` 里出现 | Step 3a 板块卡，sector_name 用 `nearest_sectors[0]` 或从 `sample_reasons` 里抽出的标准名 |
| **新事件** | 上面都不命中，但 `local_news.news` 非空（或 `db_frequency.ths_hot_reason_hits > 0`） | Step 3b 事件卡 |
| **完全不识** | 全部为空 | 视情况调一次 WebSearch 补救（限 15s），仍无 → Step 3c 兜底卡 |

## Step 3 · 出卡片

### 3a 板块卡

```python
import sys
sys.path.insert(0, "code")
from lib import sector_pack
pack = sector_pack.build_sector_pack(<canonical_sector_name>)
```

`pack["verdict_modifiers"]` 非空时结论降一档（参与→观察，观察→回避）。模板：

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

📈 技术：
   龙头位置：{MA} · 距 20 日高 {Z}%

🎯 推荐标的（Top 3-5）：
   1. {NAME1} {CODE1} · {龙头/二线} · 买点 {价位} / 止损 {价位}

⚡ 升级"参与"的信号（仅观察档）：
   · {SIGNAL_1}

⚠️ 风险：{1-2 句}
```

### 3b 事件卡

```python
from lib import sector_pack, event_pack

def categorize(text):
    # 用 LLM 跑一次 EVENT_PROMPT（见末尾），返回 {event_type, candidate_sectors, risk_sectors, core_logic}
    ...
def web_fetch(q, timeout):
    # 仅 deep 模式；调 WebSearch 工具
    ...

pack = event_pack.build_event_pack(
    "<TEXT>", mode="<MODE>", categorize=categorize,
    sector_pack_fn=sector_pack.build_sector_pack,
    web_fetch=web_fetch if MODE == "deep" else None,
)
```

模板：

```
⚡ 事件解读  [机会 / 中性 / 风险]
━━━━━━━━━━━━━━━━
📰 事件：{event_text 摘要}
   类型：{event_type} · 核心逻辑：{core_logic}

🎯 受益板块：
   1. {SECTOR1} ✓ 题材库验证 · 阶段 {...}
      龙头：{NAME} {CODE}（{N}连板）
   2. {SECTOR2} △ 近似匹配
   3. {SECTOR3} 🌐 实时验证     ← 仅 deep

⚠️ 风险板块：{...}（仅提示）

🎯 直接推荐标的：
   1. {NAME1} {CODE1} · 买点 {价位} / 止损 {价位}

💡 操作：{1-2 句}
⚠️ 风险：事件型机会通常 1-3 天兑现完毕
```

### 3c 兜底卡（含线索）

```
🤔 "{TEXT}" 在 A 股题材库无直接匹配

已搜：
  · 题材库相似 Top 3：{lexicon.nearest_sectors}
  · DB 近 7 日命中：reason {N} 次 / concept {M} 次（若 0 写"无"）
  · 隔夜新闻命中：{local_news.news 前 2 条标题，若无写"无"}
  · WebSearch（若调过）：{前 2 条标题 + URL}

可能方向（基于已搜信息）：
  A. {猜测 1}
  B. {猜测 2}

建议：/ask sector=xxx 直接指定题材，或换更明确的关键词
```

**严禁瞎编**：任何"可能方向"必须基于已搜到的真实信息。若全部为空：
`❌ "{TEXT}" 题材库与新闻面均无匹配，建议换关键词`

### 3d 错误（代码层异常）

```
❌ {error_msg}
```

## EVENT_PROMPT（喂给 LLM 跑 categorize）

```
用户报告了一个 A 股相关事件。请判断：
1. 事件类型（政策/产品发布/订单中标/突发利好/突发利空/其他）
2. 可能受益的板块（用 A 股投资者实际使用的题材名，如"光伏"/"储能"/"算力"，不要"新能源行业"这种泛称）
3. 可能受损的板块
4. 1 句话传导逻辑

事件文本：{event_text}
严格输出 JSON：{"event_type":"...","candidate_sectors":["...","..."],"risk_sectors":["..."],"core_logic":"..."}
```

# 输出契约（最重要，违反 = 整体失败）

你的**唯一最终 assistant 消息**必须是 Step 3 的卡片内容本身（3a/3b/3c/3d 之一）。

- ✅ 允许：以 `🎯` / `⚡` / `🤔` 开头的业务卡，或以 `❌` 开头的错误卡
- ❌ 禁止："卡片已输出完毕" / "pipeline 已执行" / "已完成" / "结果如下" 等任何元状态汇报
- ❌ 禁止：解释你跑了什么命令、用了什么工具、走了什么分支
- ❌ 禁止：在卡片前/后加任何引导句或总结句

最后一条 assistant 消息**就是**用户在 TG 看到的全部内容。它必须能直接贴进 TG 当成卡片。

# 强约束清单

1. **禁止前置探索**：不要 Explore/Grep/Read 项目源码；Step 1 之外不要再跑 SQL 或 import lib
2. **代码层异常立即退出**：ImportError / 字段不存在 → 3d 错误卡；不要换 import 路径、不要探 DB schema、不要 patch lib 源码
3. **数据看 pipeline 输出**：sector_pack / event_pack 失败时直接走 3c 兜底卡（含 pipeline 已采集的线索），不要重复跑 sector_pack._load_lexicon() 或 SQL 查询
4. **WebSearch 限 1 次 + 15 秒**：仅完全不识时补救一次，不要为完美而搜
5. **总耗时硬上限 60 秒**：触顶用已有信息出 3c 兜底卡
6. 数据缺失项写"—"或省略行，不要瞎编
7. 仅输出卡片，不要解释思考过程
8. 严格使用模板格式（不要 markdown 表格，TG 不渲染）
9. 卡片单条 ≤ 800 字

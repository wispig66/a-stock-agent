---
name: stock-query
description: 单股深度分析。给定一个 A 股代码（主板/创业板），按题材派框架判定值不值得买/继续持有，给买点/止损/止盈。当用户传入参数 code=XXXXXX mode=fresh|holding 时触发。
---

# stock-query · 单股决策助手

**调用方**：`stock_codex.apps.command_router` 通过 `codex exec` headless 触发；也可被 stock-ask 通过 Skill 工具转发。
**入参**：prompt 文本中以 `code=600519 mode=fresh` 形式传入。

# 工作流（按序，不跳）

> ⚠ 整个流程只有 3 步：跑 pipeline 拿 fact pack → 综合判断档位 → 出卡片。
> 直接按下方步骤执行，**不要 Explore/Grep/Read 任何项目源码或 DB schema**。
> 代码层异常（ImportError / 字段不存在）→ 立即 3d 错误卡。
> 业务层数据缺失（pipeline 某字段 `ok=false`）→ 按降档规则继续出卡。

## Step 1 · 跑 pipeline 拿 fact pack（一个命令）

```bash
.venv/bin/python scripts/stock_query_pipeline.py --code <CODE> --mode <MODE>
```

**只用这条命令**。不要 `which uv` / `command -v uv` / 探路径——`.venv/bin/python` 在项目根目录下永远存在（`scripts/setup.sh` 保证）。失败 → 直接 3d 错误卡。

stdout 是 JSON。预期 3-10 秒返回。字段说明：

| 字段 | 形态 | 用法 |
|---|---|---|
| `code` / `name` / `mode` | str | 元数据（name 取自 realtime） |
| `realtime.ok` + `data` | `{name, open, pre_close, close, high, low, vol, amount, date, time}` | 必拿。`ok=false` → 直接走 3d 错误卡 |
| `kline.data` | `[{date, open, high, low, close, vol, ma5, ma10, ma20, ...}]` 60 天 | MA 位置 / 量比 / 距 20 日高 |
| `concept.data` | `{concept_name, top_concepts: [{concept_name, pct_chg, leader_name, ...}]}` | 概念板块榜 Top 20（东财，可能 fail） |
| `money_flow.data` | `[{date, main_in, small_in, medium_in, large_in, super_in}]` 5 天 | 近 3-5 日主力资金（东财，可能 fail） |
| `news.data` | `[{title, url, date}]` 最多 10 条 | 个股近 7 日新闻（同花顺，少数情况空 []） |
| `meta.data` | `{board, is_st}` | 本地 DB，几乎不会 fail |
| `ths_hot_reasons.data` | `[{date, reason}]` 近 10 日 | 该票在 ths_hot 出现过的题材原文 |
| `holding.data` | `{code, name, genre, cost, shares, buy_date, stop_loss, take_profit, unlock_date, is_locked, note}` 或 `null` | **仅 mode=holding**。`null` 说明 holdings.yaml 里没这只 |

**任何字段 `ok=false`**：取 `error` 字段汇报"该项数据缺失"，按 Step 2 降档规则处理，**不要重跑、不要 patch lib**。

## Step 2 · 综合判断（题材派六维 + 档位）

直接读 fact pack 想清楚，**不要再调 LLM / WebSearch / 任何工具**。

### 六维判定

| 维度 | 怎么判（用哪些字段） |
|---|---|
| 题材归属 | `ths_hot_reasons.data` 取最近一条 reason 拆题材名；空 → 用 `concept.data.concept_name` 兜底；都空 → 标"无明确主线" |
| 题材位置 | `concept.data.top_concepts` 里找到本票题材，看 pct_chg 与近期表现：启动期 5–15%、主升期 >15%、高潮期 单日 >5% 或龙头连板≥4、退潮期 近 3 日累跌 |
| 个股位置 | 对比 `concept.data.top_concepts[*].leader_name` 与本票：龙头/二线/边缘（无概念数据则标"—"） |
| 资金 | `money_flow.data` 近 3 日 `main_in` 累加；正/负 + 峰值 |
| 技术 | `kline.data` 最后一行 close vs MA5/MA10/MA20；近 20 日相对高低位置；最后一日 vol / 前 5 日均量 = 量比 |
| 消息 | `news.data` 前 5 条标题，标"利好/利空/中性"；找公司公告类（标题含"公告"/"中标"/"减持"/"业绩") |

### 档位结论

**fresh 分支**：
- **买入**：题材在启动/主升 + 个股是龙头或紧跟龙头 + 资金净流入 + 技术不在高位（距 20 日高 ≥5%）
- **观察**：方向对但任一维度不达标。**必须列出"什么信号升级为买入"**（≥2 条具体可观测信号）
- **回避**：题材退潮 / 高位滞涨 / 资金持续 3 日净流出 / 技术破位，任一即触发

**holding 分支**（需 `holding.data` 非 null）：
- **加仓**：买入逻辑仍然成立 + 未到第一止盈 + 未锁仓（`is_locked=false`）
- **持有**：维持原计划；盈利 >5% 时建议止损上移到成本价
- **减仓清仓**：原买入逻辑失效（题材退潮 / 资金转流出 / 跌破关键位）

### 数据缺失的降档规则（任一命中即降一档）

- `concept.ok=false` 且无 `ths_hot_reasons`：题材判定不可信 → 买入降观察、观察降回避
- `money_flow.ok=false`：资金面盲 → 买入降观察
- `kline.ok=false`：技术面盲 → 不出买入档，最多观察
- `news.data=[]`：消息项标"—"，不降档（很常见）

### 关键价位（每档都必给）

- **买点**：限价 或 触发条件（含价位）。从 `realtime.close` 或 `kline` 关键位推
- **止损位**：具体数字 + 依据（前低 / MA20 破位 / 成本价上挪）
- **止盈位**：第一目标 / 第二目标（按概念龙头近期高点 + 个股压力位）

## Step 3 · 出卡片

### 3a fresh 模板

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

### 3b holding 模板

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

### 3c holding 模式但 holdings.yaml 里没这只

```
⚠️ {CODE} 不在 holdings.yaml 中
建议用 mode=fresh 分析，或先 /buy 录入持仓
```

### 3d 错误卡（代码层异常 / realtime 失败）

```
❌ {error_msg}
```

# 输出契约（最重要，违反 = 整体失败）

你的**唯一最终 assistant 消息**必须是 Step 3 的卡片内容本身（3a/3b/3c/3d 之一）。

- ✅ 允许：以 `📊` 开头的业务卡，或以 `❌`/`⚠️` 开头的状态卡
- ❌ 禁止："卡片已输出完毕" / "pipeline 已执行" / "已完成" / "结果如下" 等任何元状态汇报
- ❌ 禁止：解释你跑了什么命令、用了什么工具、走了什么分支
- ❌ 禁止：在卡片前/后加任何引导句或总结句

最后一条 assistant 消息**就是**用户在 TG 看到的全部内容。它必须能直接贴进 TG 当成卡片。

# 强约束清单

1. **禁止前置探索**：不要 Explore/Grep/Read 项目源码；Step 1 之外不要 import lib 或跑 SQL
2. **代码层异常立即退出**：ImportError / 字段不存在 / pipeline 返回非 JSON → 3d 错误卡；不要换 import 路径、不要探 DB schema、不要 patch lib 源码
3. **数据层 `ok=false` 走降档**：按 Step 2 降档规则继续出卡，不要重跑 fetch、不要换数据源、不要试着 curl
4. **realtime 失败 = 整体失败**：`realtime.ok=false` 时直接 3d，不出业务卡
5. **总耗时硬上限 60 秒**：触顶用已有信息出最稳妥档位卡（宁可"观察"也不空跑）
6. 数据缺失项写"—"或省略行，不要瞎编
7. 仅输出卡片，不要解释思考过程
8. 严格使用模板格式（不要 markdown 表格，TG 不渲染）
9. 卡片单条 ≤ 800 字
10. 价位必须给数字（非区间），止盈最多两档

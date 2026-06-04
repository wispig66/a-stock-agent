---
name: stock-weekly
description: A 股周日晚 21:00 触发的周复盘 + 下周方向。Part 1 本周市场叙事 + 个人交易回顾，Part 2 周末消息归类 + 下周 2-3 条主线（含代表股、催化、风险）。落地长文 data/weekly_review/YYYY-WW.md 供下周 L1-L4 读取。当用户要求"周复盘"、"周报"、"weekly"、"下周看什么"、"下周方向"，或周日晚自然触发时调用。
---

## Codex automation 契约

本 skill 会被 Codex automation 无人值守触发。执行时必须产出下面列出的文件和推送副作用；不要只回复完成。如果任一步骤失败，必须说明具体失败步骤，并停止声称成功。

- 必须先检查本周 `data/weekly_review/YYYY-WW.md` 是否已存在。
- 已存在且未 force 时必须跳过并报告，不重复推送。
- 需要生成周报时必须写入 `data/weekly_review/YYYY-WW.md` 和 `data/last_weekly_card.md`。
- 必须通过 `.agents/skills/stock-premarket/scripts/push.py --source stock-weekly` 推送摘要。
- 输出层级：文件内容是完整卡片/长文，IM 推送是摘要卡片，Codex automation 最终回复只给简要运行摘要。

# 定位

L7 望远镜层，周维度。**不给买点**（买点交给 L1 周一早上）。两段输出：

- Part 1 本周复盘 — 情绪周期定位 + 主线梳理 + 资金面 + 情绪指标 + 题材轮动 + 个人交易回顾（6 节叙事，禁止堆数据）
- Part 2 下周方向 — 题材库校准 + 主线延续/切换判断 + 2-3 条主线 × 3-5 只代表股 + 关键催化时点 + 风险 + 操作纪律

继承 [[feedback-postmarket-style]]（叙事不堆数据）+ [[feedback-news-awareness]]（消息面独立成段）。

# 工作流（5 步）

# 输出契约（最重要，违反 = 整体失败）

**所有数据必须有来源支撑**（[[feedback-data-must-be-sourced]]）。

`aggregate.py` stdout 末尾输出 `=== ALLOWED === { ... } === /ALLOWED ===` JSON，
是**该次周复盘卡片/长文唯一允许引用的事实清单**：

- 6 位股票代码 → 必须在 `ALLOWED.codes` keys
- 中文股票名（stock_basic 匹配）→ 必须在 `ALLOWED.codes` values
- 周涨跌幅 "±X.X%" → 必须在 `ALLOWED.pct[code] ± 0.5%`（来自 top_gainers）
- "最高 N 板 / 涨停 N 只" → 来自 `ALLOWED.summary.{max_consec_week, max_limit_up_week}`
- 题材名 → 应在 `ALLOWED.concepts`（v1 不强校验）

**禁止**：从训练数据印象编出本周未出现的股票/题材。Step 2 WebSearch 抓到的新闻条目可以直接引用 URL，但**不能**根据新闻编造个股 X 涨 Y%——任何具体股票/数字必须来自 Step 1 fact pack。

`push.py` 自动跑校验器（warn 模式留日志；enforce 模式拒推）。

写入文件的内容必须是周复盘长文，IM 推送必须是摘要卡片；Codex automation 最终回复只给简要运行摘要，不要用“完成”替代文件写入和推送。

## Step 0 · 幂等检查（先于聚合/Web）

先算本周 `week_label`，检查 `data/weekly_review/YYYY-WW.md` 是否已存在。存在且未 force 时，直接停止：不跑 aggregate、不做 WebSearch、不写 `data/last_weekly_card.md`、不推送 IM，只在 Codex automation 最终回复中报告已跳过。

```python
from datetime import date, timedelta
from pathlib import Path

today = date.today()
weekday = today.weekday()
if weekday == 6:
    friday = today - timedelta(days=2)
elif weekday >= 4:
    friday = today - timedelta(days=weekday - 4)
else:
    friday = today - timedelta(days=weekday + 3)
iso_year, iso_week, _ = friday.isocalendar()
week_label = f"{iso_year}-W{iso_week:02d}"
out = Path("data/weekly_review") / f"{week_label}.md"
if out.exists():
    print(f"SKIP: {out} 已存在；未 force 不重复生成或推送")
```

## Step 1 · 本地数据聚合

```bash
.venv/bin/python .agents/skills/stock-weekly/scripts/aggregate.py
```

读完整 stdout。这是本周 fact pack（情绪曲线 / 周涨幅榜 / 同花顺题材 / 龙虎榜席位 / 个人交易 / 周内异动日报文件路径 + raw JSON + ALLOWED）。

raw JSON 末尾包含完整 pack，下游 Step 5 渲染长文时会复用。ALLOWED 段是事实白名单（见上方"输出契约"）。

## Step 2 · Web 增量（周末消息）

时间窗 = 上周六 00:00 ~ 本周日 21:00。**必须**调用 WebSearch 四次（可并行思考，但工具调用各自一次），分别取以下四桶。每桶取头部 5-10 条，要"标题 + 时间 + 一句话总结"，不要正文照搬：

1. **政策** — 国常会 / 央行 / 证监会 / 监管夜间公告 / 重要会议（"本周末 国常会 政策 A股"）
2. **产业催化** — 行业会议 / 重大订单 / 业绩预告 / 新产品发布（"本周末 产业 业绩 订单"）
3. **海外** — 美股周五收盘 / 美债 / 地缘 / 商品价格（"本周末 美股 美债 地缘"）
4. **监管** — IPO / 再融资 / 减持 / 退市新规（"本周末 IPO 减持 退市"）

任一桶失败：记下来，下游 `web_status=degraded`，不阻塞流程。

## Step 2.5 · 异动日报回顾（周内 5 天）

Step 1 fact pack 给的 `anomaly_files` 是周内每天的异动日报路径。**逐一读取**（如果 ≤ 5 个，逐个 Read；如果有缺失天，跳过）。
目的：识别周内异动的题材聚类，与 Step 2 的周末消息交叉验证。

## Step 3 · 本周复盘叙事化（Part 1）

按 6 节生成 markdown，输出到内部变量 `part1_narrative`（不要打印到用户，只 Step 5 渲染时用）：

1. **情绪周期定位** — fact pack 的 `sentiment_series` 看 5 天 phase 变化，写一句话总结
2. **主线梳理** — `top_gainers` 取前 5 + `ths_hot_reasons` 聚类，写"X 板块本周累计涨 Y%，龙头 Z 完成几连板，逻辑是 W"
3. **资金面** — `lhb_seats` 前 5 + 主力净流入（如果 anomaly_files 里提到），简评
4. **情绪指标** — `limit_up_ladder` 看 5 天连板高度趋势，配合涨跌停家数
5. **题材轮动** — 对比 Step 2 周末消息 + Step 2.5 周内异动，叙事"资金从 X 流向 Y / X 退潮 / Y 在酝酿"
6. **本周交易回顾**：
   - 若 `weekly_trades` 非空：每笔写"买点逻辑（reason）→ 实际走势（拉 daily_kline 看后续价格）→ 结论（兑现/止损/错过）" + 识别重复模式
   - 若 `weekly_trades` 为空：退化为「空仓观察笔记」，假设按上周末 weekly_review themes 买入的回报

## Step 4 · 下周方向研判（Part 2）

1. **题材库校准** — 调 `stock_codex.market.event_pack.calibrate()` 把 Step 2 的事件归到现有题材或新建。代码：

   ```python
   from stock_codex.market.event_pack import calibrate
   # 把 Step 2 抓到的 N 个事件主题名传入
   themes_check = calibrate(extracted_theme_names, lexicon=...)
   ```

   （若调用复杂，可在 part2_narrative 文字里直接给映射，不强求调用）

2. **主线延续 vs 切换** — 基于情绪周期 + 周末消息，给"延续 / 切换 / 高位防守 / 观察"判断
3. **2-3 条主线** — 每条给：
   - `name` 主线名
   - `stance` 立场（延续 / 切换 / 高位防守 / 观察）
   - `leaders` 代表股代码 3-5 只（从 top_gainers + ths_hot_reasons 挑）
   - `catalysts` 关键催化（日期 + 事件）
   - `risks` 主要风险
   - `match_score` 与历史题材库匹配度（high/mid/low）
4. **操作纪律** — 派别纪律提醒，例："龙头股加速期不追"、"高度板回踩 5 日线再战"。**不给具体买点**
5. 把结构化的 themes / discipline_notes / web_status 装到 `parts` dict（下面渲染用）

## Step 5 · 双输出

### 5.1 落地长文

```python
from datetime import date
from pathlib import Path
from stock_codex.market.weekly_pack import build_weekly_data_pack, render_long_form

pack = build_weekly_data_pack(end_date=date.today())
parts = {
    "part1_narrative": "<Step 3 输出>",
    "part2_narrative": "<Step 4 叙事>",
    "themes": [<Step 4.3 结构化>],
    "discipline_notes": "<Step 4.4>",
    "web_status": "ok" | "degraded",
}
md = render_long_form(pack, parts)
out = Path("data/weekly_review") / f"{pack['week_label']}.md"
out.parent.mkdir(parents=True, exist_ok=True)
if out.exists():
    raise SystemExit(f"SKIP: {out} 已存在；未 force 不覆盖")
out.write_text(md)
print(f"WROTE: {out}")
```

### 5.2 TG 摘要卡（1500-2500 字）

Write 摘要卡到 `data/last_weekly_card.md`，然后用统一 push.py：

```bash
.venv/bin/python .agents/skills/stock-premarket/scripts/push.py \
    --file data/last_weekly_card.md --source stock-weekly
```

`push.py` 会跑 card_validator 对照 `data/allowed_latest_stock-weekly.json`（warn 模式留审计日志，enforce 模式拒推）。**不要**直接调 `stock_codex.infra.notify` 或 `_tg_send`（绕过校验）。

模板：

```
🗓 周复盘 {week_label}（{monday} - {friday}）

【本周复盘】
情绪周期：{stage_now} ({stage_trend})
本周主线：{top_3_themes_with_pct}
退潮：{declining_themes}
连板高度：{ladder_trend}
个人交易：本周 {n} 笔, {w}胜{l}负{e}平
关键复盘点：{key_lesson_one_line}

【下周方向】
{themes_rendered}  ← 主线 1/2/3 每条 3-5 行

操作纪律：{discipline_notes}

🔗 完整复盘 data/weekly_review/{week_label}.md
```

# 边界

- 节假日周（交易日 < 5）：fact pack `trading_days_in_week` 会反映；模板里加 banner "⚠ 本周仅 X 个交易日，下周判断置信度降低"。春节后/国庆后第一周尤其要标注"风格可能剧变"
- 空仓周：Part 1 第 6 节退化为"空仓观察笔记"
- Web 全部失败：`web_status=degraded`，Part 2 只用本地数据 + ths_hot_reason 推方向
- 当周长文已存在：未 force 时跳过并报告，不重复推送；只有用户明确 force 时才覆盖当周文件。

# 关键约束

- 长文 落本地 OK；TG 卡只给逐笔汇总（"本周 3 笔 2 胜 1 负"），不要把 trades 表的逐笔金额发到 TG
- machine-readable YAML 字段名严格按 spec §5 写死，L1 Step 1.5 按字段名解析

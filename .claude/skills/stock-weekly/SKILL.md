---
name: stock-weekly
description: A 股周日晚 21:00 触发的周复盘 + 下周方向。Part 1 本周市场叙事 + 个人交易回顾，Part 2 周末消息归类 + 下周 2-3 条主线（含代表股、催化、风险）。落地长文 data/weekly_review/YYYY-WW.md 供下周 L1-L4 读取。当用户要求"周复盘"、"周报"、"weekly"、"下周看什么"、"下周方向"，或周日晚自然触发时调用。
---

# 定位

L7 望远镜层，周维度。**不给买点**（买点交给 L1 周一早上）。两段输出：

- Part 1 本周复盘 — 情绪周期定位 + 主线梳理 + 资金面 + 情绪指标 + 题材轮动 + 个人交易回顾（6 节叙事，禁止堆数据）
- Part 2 下周方向 — 题材库校准 + 主线延续/切换判断 + 2-3 条主线 × 3-5 只代表股 + 关键催化时点 + 风险 + 操作纪律

继承 [[feedback-postmarket-style]]（叙事不堆数据）+ [[feedback-news-awareness]]（消息面独立成段）。

# 工作流（5 步）

## Step 1 · 本地数据聚合

```bash
uv run .claude/skills/stock-weekly/scripts/aggregate.py
```

读完整 stdout。这是本周 fact pack（情绪曲线 / 周涨幅榜 / 同花顺题材 / 龙虎榜席位 / 个人交易 / 周内异动日报文件路径 + raw JSON）。

raw JSON 末尾包含完整 pack，下游 Step 5 渲染长文时会复用。

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

1. **题材库校准** — 调 `code/lib/event_pack.calibrate()` 把 Step 2 的事件归到现有题材或新建。代码：

   ```python
   from lib.event_pack import calibrate
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
from lib.weekly_pack import build_weekly_data_pack, render_long_form

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
out.write_text(md)
print(f"WROTE: {out}")
```

### 5.2 TG 摘要卡（1500-2500 字）

直接调 `scripts/tg_listener._tg_send`（python -c）或用 `notify.sh` 包装。

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
- 当周长文已存在：weekly_loop.py 跳过；本 skill 被直接手动调时，**覆盖**当周文件（用户预期是手动 force）

# 关键约束

- 长文 落本地 OK；TG 卡只给逐笔汇总（"本周 3 笔 2 胜 1 负"），不要把 trades 表的逐笔金额发到 TG
- machine-readable YAML 字段名严格按 spec §5 写死，L1 Step 1.5 按字段名解析

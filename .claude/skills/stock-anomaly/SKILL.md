---
name: stock-anomaly
description: A 股全市场异动汇总 + 题材发现。读最近 N 分钟 anomaly_loop 推送（火箭发射/封板/炸板/60日新高），按题材聚类，叠加财联社/同花顺热点新闻，输出叙事卡片 + Telegram 推送。回答"现在场内有什么新方向"、"哪个题材在冒头"、"新主线有没有候选"。中间时段单条异动告警走 daemon 自动推送，不调本 skill。当用户在盘中要求"异动"、"anomaly"、"现在场内"、"新主线"、"什么在涨"、"哪些方向冒头"、"新候选"，或自然语言"A股短线 看看新机会"时调用此 skill。
metadata:
  type: skill
---

# stock-anomaly · 全市场异动汇总

## 与其他 skill 的边界

- **watch_loop.py**（盘中常驻）：盯今早观察池 + holdings 已知名单的 6 种触发
- **anomaly_loop.py**（盘中常驻）：扫全市场新冒头票，自动排除观察池+holdings，单条推 Telegram
- **本 skill**：用户问"现在有什么新方向"时跑一次，把 anomaly_loop 最近 30 分钟的散点推送**聚成叙事**，叠加消息面，给出"是否值得加入观察池 / 是否提示新主线切换"的判断

## 触发场景

- 用户喊："看看现在场内有什么异动" / "新主线候选" / "哪个方向在冒头"
- 盘中任意时段（非固定时点）；若 anomaly_loop 没在跑，先提示用户启动 `bash code/run_anomaly_loop.sh`

## Step 1 · 拉最近 30 分钟 anomaly_loop 推送

```bash
sqlite3 data/daily.db "SELECT timestamp, text FROM push_log
  WHERE source='stock-anomaly'
    AND datetime(timestamp) >= datetime('now', 'localtime', '-30 minutes')
  ORDER BY id ASC;"
```

若返回 0 条：先问用户 anomaly_loop 是否在跑（`pgrep -f anomaly_loop`），未跑则提示 `bash code/run_anomaly_loop.sh` 启动后再重跑。

## Step 2 · 聚类成结构化清单

把每条推送拆成 `(代码, 名称, 异动类型, 时间)`，**按 6 位代码所属概念聚类**——必要时调一次 `ak.stock_individual_info_em(symbol=code)` 拿所属板块/概念。

聚类目标：
- 同一题材至少 3 只票冒头 → 标记为"新方向候选"
- 同一只票多类型连环触发（如"火箭发射 → 封涨停"） → 标记为"强势单票"
- 炸板集中爆发（10 分钟内 ≥ 5 只炸板） → 标记为"情绪冷却信号"

## Step 3 · 叠加消息面（继承 [[feedback-news-awareness]]）

**3a · 本地：同花顺 reason tag 交叉验证**（零网络成本，先做）

```bash
sqlite3 data/daily.db "SELECT name, code, reason FROM ths_hot_reason
  WHERE date = (SELECT max(date) FROM ths_hot_reason)
  ORDER BY change_pct DESC LIMIT 30;"
```

把异动聚类出的 Top 3 题材去匹配 reason tag：命中的标 🔥（reason 驱动验证），未命中的标 🆕（纯异动冒头、无 D-1 验证、降一档信号强度）。

**3b · 远程：财联社电报对照**

对 Top 3 题材每个调 WebFetch 拉财联社电报：

```
https://www.cls.cn/telegraph
```

每条题材新闻必须标注：
- 是否对应今日已推涨停潮（驱动验证 ✅）
- 是否仅个股传闻（弱信号 ⚠️）
- 是否与今早 L1 观察池题材冲突（主线切换风险 🔄）

## Step 4 · 输出叙事卡片

**空状态优先判断**：若 Step 2 聚类后没有任何题材达到"≥3 只异动"门槛，**禁止**为了出卡片硬凑方向。直接走下方"无聚集模板"，不要走完整模板。

**无聚集模板**（题材聚类 0 个时用）：

```markdown
🆕 <b>盘中异动汇总 · HH:MM</b>（最近 30 分钟）

🌡️ <b>异动密度</b>：火箭 N1 · 封板 N2 · 炸板 N3 · 60日新高 N4

⚪ <b>本时段无明显题材聚集</b>
当前异动散乱、无 ≥3 只同题材冒头，资金未形成合力。建议继续观望，30 分钟后再看。
（强势单票 / 炸板潮 / 持仓利空 命中时仍按下方分块输出，但不要造"新方向"。）

———
本系统纪律：无聚集不强行造方向 · 异动需 ≥3 票同题材才算冒头
```

**完整模板**（题材聚类 ≥1 个时用）：

```markdown
🆕 <b>盘中异动汇总 · HH:MM</b>（最近 30 分钟）

🌡️ <b>异动密度</b>：火箭 N1 · 封板 N2 · 炸板 N3 · 60日新高 N4

🔥 <b>冒头新方向</b>
1. [题材名] · N 只异动 · 龙头 600xxx XXX
   驱动：[财联社/同花顺/无明确催化]
   信号强度：⭐⭐⭐ / ⭐⭐ / ⭐
   建议：[加入观察池 / 仅盯不动 / 跳过]

📈 <b>强势单票</b>（多类型连环触发）
- 600xxx XXX · 火箭 → 封板 · 题材 [xx]

💥 <b>情绪信号</b>
- 炸板潮：[有/无]，N 只 10 分钟内炸板，主线[xx]开始分歧
- 60日新高：[有/无]，N 只突破，资金切换迹象

📰 <b>消息面对照</b>
- [新闻标题] → 对应 [题材]，驱动验证 [✅/⚠️/🔄]

⚠️ <b>明日观察池调整建议</b>
- 新增：[xxx]（理由：题材冒头 + 资金验证）
- 替换：[xxx → yyy]（理由：原票炸板，新票封板）
- 保持：纪律未触发的不动
```

## Step 5 · 推送 + 入库 + 持久化给次日 L1

**5a · 落盘文件（给次日 L1 复评用）**

把卡片**同时** Write 一份到 `data/anomaly_findings/YYYYMMDD.md`（YYYYMMDD = 今日交易日）。
- 同一天多次调用 → 后写覆盖前写（最后一次的快照即可，L1 关心的是"昨日尾盘异动出了啥"）
- 目录不存在时先 `mkdir -p data/anomaly_findings`

L1 SKILL.md Step 1 已被改为：若 `data/anomaly_findings/<前一交易日>.md` 存在，读进来作为"昨日异动新主线候选"输入，**不**自动加入观察池，但允许 LLM 在题材判断时引用。

**5b · Telegram 推送**

```bash
uv run code/notify.py --source stock-anomaly-summary < /tmp/anomaly_card.md
```

## 纪律

1. **不替代 L1/L4 决策**：本 skill 只发现"线索"，加不加进观察池由次日 L1 复评
2. **冷却期**：同一题材一交易日内最多调用 2 次，避免反复看异动追涨
3. **必须叠加消息面**：纯数据冒头不可信，没新闻支撑的题材冒头打折扣
4. **持仓利空优先**：若 anomaly 推送里命中 holdings 任一只的炸板，卡片头部红色提示

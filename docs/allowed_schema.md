# ALLOWED fact-pack schema

每个 stock skill 的 pipeline 脚本（fetch_realtime.py / fetch_postmarket.py /
stock_query_pipeline.py / stock_ask_pipeline.py / fetch_data.py / aggregate.py /
anomaly_loop.py）必须在 stdout 末尾输出一段以 `=== ALLOWED ===` 分隔的 JSON
块，列出该次卡片**唯一允许引用的事实**。

格式：

```
... pipeline 正常输出 ...

=== ALLOWED ===
{
  "schema_version": "1",
  "skill": "stock-intraday",
  "snapshot_at": "2026-05-18T14:30:00+08:00",
  "codes": {"001259": "利仁科技", "603082": "北自科技", ...},
  "lianban": {"001259": 6, "600578": 4, "603082": 3, ...},
  "pct": {"603082": 10.0, "002407": 2.60, ...},
  "summary": {
    "limit_up": 78,
    "broken": 36,
    "amount_yi": 25788,
    "date": "2026-05-18"
  },
  "concepts": ["AI算力", "氟化工", "人形机器人", "电力"],
  "news": [
    {"title": "国务院印发《稳岗扩容提质行动方案》", "url": "...", "time": "14:20"},
    ...
  ],
  "global_markets": {
    "KOSPI": -3.5,
    "纳指": -1.54,
    "美30年期国债": 5.12
  }
}
=== /ALLOWED ===
```

## 字段说明与必填性

| 字段 | 类型 | 必填 | 用法 | 校验规则 |
|---|---|---|---|---|
| `schema_version` | str | ✅ | 兼容性标记 | — |
| `skill` | str | ✅ | 来源 skill 名 | — |
| `snapshot_at` | ISO8601 str | ✅ | fact pack 时点 | — |
| `codes` | dict code→name | ✅ | 该卡允许出现的全部股票（含连板/炸板池 + 观察池 + 持仓 + 概念龙头） | 卡里任何 6 位数字必须在 keys；任何中文股名（stock_basic.name 命中）必须在 values |
| `lianban` | dict code→int | 可选 | 连板数 | 卡里 `N 板`/`N 连板` 紧邻 code 时校验等于 |
| `pct` | dict code→float | 可选 | 涨跌幅（百分比，2 位精度） | 卡里 `±X.X%` 紧邻 code 时校验 ±0.5% 容差 |
| `summary.limit_up` | int | 可选 | 当日涨停总数 | 卡里"涨停 N 只"必须精确等于 |
| `summary.broken` | int | 可选 | 当日炸板总数 | 卡里"炸板 N 只"必须精确等于 |
| `summary.amount_yi` | float | 可选 | 两市成交额（亿元） | 暂不强校验 |
| `summary.date` | "YYYY-MM-DD" | ✅ | 数据归属日 | — |
| `concepts` | list[str] | 可选 | 今日热门题材名 | 暂不强校验（v1） |
| `news` | list[{title,url,time}] | 可选 | 该时窗内抓到的全部新闻 | 卡里类新闻行的 SequenceMatcher 相似度必须 ≥0.7 |
| `global_markets` | dict ticker→pct | 可选 | KOSPI / 纳指 / 美债等 | 暂不强校验（v1） |

## 谁是 ALLOWED 的"事实"

ALLOWED 段是 pipeline 自己拉到的事实快照。**模型不得在卡片里添加任何不在
ALLOWED 里的数据点**（参见 [[feedback-data-must-be-sourced]]）。

具体绑定（v1 强制）：

1. **股票代码**：卡片任何位置出现的 6 位数字（非小数、非时间）必须在
   `codes` keys。
2. **股票名称**：卡片中 stock_basic.name 全表（5200+ 条）匹配的中文 3+
   字 token 必须在 `codes` values。
3. **连板数**：紧邻 code 的"N 板"/"N 连板"必须等于 `lianban[code]`。
4. **涨跌幅**：紧邻 code 的"±X.X%"必须在 `pct[code] ± 0.5%`。
5. **涨停/炸板总数**：必须精确等于 `summary.limit_up` / `summary.broken`。
6. **新闻条目**：含"→"或日期数字的疑似新闻行，与 `news[].title` 的最大
   SequenceMatcher ratio 必须 ≥ 0.7。

v2 待加：concepts 强校验、global_markets 强校验。

## 验证流程

```python
from lib.card_validator import validate_card, load_stock_name_dict

stock_dict = load_stock_name_dict("data/daily.db")
ok, violations = validate_card(card_text, allowed, stock_name_dict=stock_dict)
if not ok:
    # mode=warn: 写审计日志，照样推
    # mode=enforce: 拒推 + 推一条 ⚠️ 拦截卡 + 落审计 data/card_violations/<ts>.json
    ...
```

## 模式切换

`CARD_VALIDATOR_MODE` 环境变量：
- `warn` (默认)：只写审计日志 `data/card_violations/<ts>_<source>.json`，原卡照推。**用于上线初期 1 周收集误伤数据。**
- `enforce`：失败拒推原卡，改推一条 ⚠️ 错误卡含违规摘要 + 提示查日志。

## 添加新 pipeline 时

新 fetch_* / pipeline 脚本最末尾必须 print：

```python
print("\n=== ALLOWED ===")
print(json.dumps(allowed, ensure_ascii=False, indent=2))
print("=== /ALLOWED ===")
```

tg_listener 提卡前从 subprocess stdout 抽这一段反序列化，传给 validate_card。

"""card_validator regression: 把 2026-05-18 14:30 的污染卡当 known-failing case。

5 处虚构必须被抓：
  · 大唐发电（601991）未在 ALLOWED.codes
  · 中船特气（688146）未在 ALLOWED.codes
  · 涨停 75 只 ≠ 实际 78
  · 炸板 38 只 ≠ 实际 36
  · 多氟多 +3.62% vs 实际 +2.60%（差 1.02% > 0.5%）
  · 金石资源 +4.76% vs 实际 +5.55%（差 0.79% > 0.5%）

数据源：今天 fetch_realtime --endday 实测 + lib.query.fetch_realtime 抽样。
"""
from __future__ import annotations
import pytest
from stock_codex.market.card_validator import (
    validate_card, format_violations, PCT_TOLERANCE,
)


# 今日（2026-05-18）的真实事实
TODAY_ALLOWED = {
    "schema_version": "1",
    "skill": "stock-intraday",
    "snapshot_at": "2026-05-18T14:30:00+08:00",
    "codes": {
        "001259": "利仁科技",
        "600578": "京能电力",
        "603779": "威龙股份",
        "603082": "北自科技",
        "603311": "金海高科",
        "002374": "中锐股份",
        "601133": "柏诚股份",
        "601678": "滨化股份",
        "002421": "达实智能",
        "002407": "多氟多",
        "603505": "金石资源",
        "603386": "骏亚科技",
    },
    "lianban": {
        "001259": 6, "600578": 4, "603779": 4, "603082": 3,
        "603311": 2, "002374": 2, "601133": 2, "601678": 2, "002421": 2,
    },
    "pct": {
        "603082": 10.00, "002407": 2.60, "603505": 5.55, "603386": -2.48,
        "001259": 10.00, "600578": 9.99,
    },
    "summary": {"limit_up": 78, "broken": 36, "date": "2026-05-18"},
    "concepts": ["AI算力", "氟化工", "人形机器人", "电力"],
    "news": [
        {"title": "国务院印发《稳岗扩容提质行动方案》", "url": "...", "time": "14:20"},
        {"title": "MLCC 价格反转向上，AI 芯片需求驱动", "url": "...", "time": "13:00"},
        {"title": "韩企涨价 40% 采购中国氢氟酸持续发酵", "url": "...", "time": "11:00"},
    ],
}


# 完整污染卡（即用户在 TG 看到的那条 14:30 尾盘卡，HTML 标签已剥简）
POLLUTED_CARD_14_30 = """🌤️ 尾盘复盘 · 14:30 · 2026-05-18

📖 今日市场怎么走

典型的"上午普涨→午后杀跌"分化格局。上午超100股涨停、3000+飘红，创业板一度+0.3%；午后资金大幅撤退，创业板跌逾1%、超3500股下跌。涨停75只 / 炸板38只（炸板率51%），比上午的24%急剧恶化。最高连板利仁科技6板维持，但大唐发电6连板早盘涨停后炸板收跌2.3%——高度梯队出现裂缝，情绪见顶分歧信号明确。成交额25788亿放量1522亿，资金集中在主线、非主线被抛弃。

📰 09:30-14:30 消息热点（3条命中）

- 14:20 国务院印发《稳岗扩容提质行动方案》 → 提到"人工智能+"行动，间接利好人形机器人
- 盘中 MLCC价格反转向上 → AI芯片需求驱动，利好算力硬件
- 韩企涨价40%采购中国氢氟酸持续发酵 → 直接利好多氟多/金石资源

🔥 题材延续判断

✅ 人形机器人/自动化：北自科技3板涨停封死
✅ 氟化工/工业气体：多氟多+3.62%、金石资源+4.76%、中船特气20%涨停新高 → 龙头延续
🆕 电力/AI能源：京能电力4板+AI能源双向赋能政策

💼 观察池现状（今日空仓，无持仓）

- 603082 北自科技（买点42.72）现价46.53 +10.00%
- 002407 多氟多 现价37.79 +3.62%（最高39.71）
- 603505 金石资源 现价21.34 +4.76%
- 603386 骏亚科技 现价16.47 -2.66%

⚠️ 风险提示

- 大唐发电6板断板+午后3500股下跌：明日若利仁科技也断板，情绪加速退潮
"""

# 干净版（手工修正全部 6 处虚构）
CLEAN_CARD_14_30 = POLLUTED_CARD_14_30 \
    .replace("但大唐发电6连板早盘涨停后炸板收跌2.3%——", "") \
    .replace("涨停75只 / 炸板38只（炸板率51%）", "涨停78只 / 炸板36只（炸板率46%）") \
    .replace("中船特气20%涨停新高 → ", "") \
    .replace("多氟多+3.62%", "多氟多+2.60%") \
    .replace("金石资源+4.76%", "金石资源+5.55%") \
    .replace("现价37.79 +3.62%", "现价37.42 +2.60%") \
    .replace("现价21.34 +4.76%", "现价21.50 +5.55%") \
    .replace("大唐发电6板断板+", "")


def test_polluted_card_blocked():
    ok, violations = validate_card(POLLUTED_CARD_14_30, TODAY_ALLOWED, stock_name_dict={
        "大唐发电": "601991", "中船特气": "688146", "蒙娜丽莎": "002918",
        "利仁科技": "001259", "京能电力": "600578", "北自科技": "603082",
        "多氟多": "002407", "金石资源": "603505", "骏亚科技": "603386",
    })
    print("\n违规列表：\n" + format_violations(violations))
    assert not ok, "污染卡必须被拦截"

    kinds = [v.kind for v in violations]
    targets = [v.target for v in violations]

    # 至少抓住这几条已知虚构（不要求 exact，但 must include）
    assert any("大唐发电" in t for t in targets), "未抓到大唐发电"
    assert any("中船特气" in t for t in targets), "未抓到中船特气"
    assert any("75" in t and "涨停" in t for t in targets), "未抓到涨停 75 只偏差"
    assert any("38" in t and "炸板" in t for t in targets), "未抓到炸板 38 只偏差"
    assert any("002407" in t and "3.62" in t for t in targets), "未抓到多氟多 pct 偏差"
    assert any("603505" in t and "4.76" in t for t in targets), "未抓到金石资源 pct 偏差"


def test_clean_card_passes():
    ok, violations = validate_card(CLEAN_CARD_14_30, TODAY_ALLOWED, stock_name_dict={
        "利仁科技": "001259", "京能电力": "600578", "北自科技": "603082",
        "多氟多": "002407", "金石资源": "603505", "骏亚科技": "603386",
    })
    if not ok:
        print("\n意外违规：\n" + format_violations(violations))
    assert ok, f"干净卡不应被拦截：{violations}"


def test_pct_tolerance_boundary():
    """±0.5% 容差边界。"""
    card_at_edge = "603082 北自科技 现价46.53 +10.49%"
    allowed = {"codes": {"603082": "北自科技"}, "pct": {"603082": 10.00}}
    ok, _ = validate_card(card_at_edge, allowed)
    assert ok, "0.49% 偏差应通过"

    card_over_edge = "603082 北自科技 现价46.53 +10.51%"
    ok2, v2 = validate_card(card_over_edge, allowed)
    assert not ok2 and any(x.kind == "pct_mismatch" for x in v2)


def test_summary_count_exact():
    """summary 总数零容差。"""
    allowed = {"codes": {}, "summary": {"limit_up": 78, "broken": 36}}
    ok, v = validate_card("今日涨停 77 只 / 炸板 36 只", allowed)
    assert not ok and any("77" in x.target for x in v)
    ok2, _ = validate_card("今日涨停 78 只 / 炸板 36 只", allowed)
    assert ok2


def test_future_tense_lianban_not_flagged():
    """'001259 6 板能否 7 板' — '7 板'是预测，不应当作事实断言。

    5/18 事故：盘后卡片里"6 板能否 7 板""断 5 板""跨级 4 板"被全部抓成 lianban_mismatch。
    """
    card = "001259 利仁科技 6 板能否 7 板，封死则带动情绪；断 5 板则梯队彻底崩"
    allowed = {
        "codes": {"001259": "利仁科技"},
        "lianban": {"001259": 6},
    }
    ok, v = validate_card(card, allowed)
    # 应通过：'6 板'命中真值，'7 板'是预测，'5 板'前有'断'
    lianban_violations = [x for x in v if x.kind == "lianban_mismatch"]
    assert not lianban_violations, f"未来时/差值上下文不应触发 lianban_mismatch: {lianban_violations}"


def test_future_tense_pct_not_flagged():
    """'000988 能否高开 +2%' — '+2%'是预测，不应当作今日涨幅。"""
    card = "000988 华工科技 明日能否高开 +2% 以上且不破昨收"
    allowed = {
        "codes": {"000988": "华工科技"},
        "pct": {"000988": 10.00},
    }
    ok, v = validate_card(card, allowed)
    pct_violations = [x for x in v if x.kind == "pct_mismatch"]
    assert not pct_violations, f"未来时上下文不应触发 pct_mismatch: {pct_violations}"


def test_position_size_pct_not_flagged():
    """'仓位 10%'是执行参数，不是股票当日涨幅。"""
    card = "000988 华工科技 · 仓位 10%"
    allowed = {
        "codes": {"000988": "华工科技"},
        "pct": {"000988": 2.00},
    }
    ok, v = validate_card(card, allowed)
    pct_violations = [x for x in v if x.kind == "pct_mismatch"]
    assert not pct_violations, f"仓位百分比不应触发 pct_mismatch: {pct_violations}"


def test_news_entity_name_not_flagged():
    """'鸿博股份子公司英博数科债务逾期' — 新闻里出现的公司不应被当作虚构股名。"""
    card = "17:23 鸿博股份子公司英博数科债务逾期，公司承担连带责任 → 算力租赁分支利空"
    allowed = {"codes": {"000988": "华工科技"}}  # 鸿博股份不在
    ok, v = validate_card(card, allowed, stock_name_dict={
        "华工科技": "000988", "鸿博股份": "002229",
    })
    name_violations = [x for x in v if x.kind == "unknown_name"]
    assert not name_violations, f"新闻实体不应触发 unknown_name: {name_violations}"


def test_lianban_name_anchor_overrides_prior_code():
    """'... 001259 利仁科技 ... 4 板京能电力孤军' — 应锚到 600578(京能=4板) 而非 001259。"""
    card = "最高 6 板 001259 利仁科技独苗；🟡 电力：4 板京能电力孤军"
    allowed = {
        "codes": {"001259": "利仁科技", "600578": "京能电力"},
        "lianban": {"001259": 6, "600578": 4},
    }
    ok, v = validate_card(card, allowed)
    assert ok, f"name-anchor 应让 4 板绑到 600578(=4)，不报错: {v}"


def test_lianban_aggregate_distribution_skipped():
    """'4 板只剩 2 只' — 是梯队分布聚合，不应当作单股 4 板事实。"""
    card = "001259 利仁科技 6 板独苗；4 板只剩 2 只；中间 5 板真空"
    allowed = {
        "codes": {"001259": "利仁科技"},
        "lianban": {"001259": 6},
    }
    ok, v = validate_card(card, allowed)
    assert ok, f"聚合分布表述不应触发: {v}"


def test_data_source_vendor_name_not_flagged():
    """'同花顺 reason 标签' — 数据源名同名个股(300033)，不应被当作虚构。"""
    card = "同花顺 reason 标签商业航天 10 次居首"
    allowed = {"codes": {"000988": "华工科技"}}
    ok, v = validate_card(card, allowed, stock_name_dict={
        "华工科技": "000988", "同花顺": "300033",
    })
    name_violations = [x for x in v if x.kind == "unknown_name"]
    assert not name_violations


def test_unknown_code_in_holding_section():
    """卡里出现 ALLOWED 外的代码 → 抓住。"""
    card = "🎯 持仓：601991 大唐发电 6 连板 +10%"
    allowed = {"codes": {"600578": "京能电力"}}
    ok, v = validate_card(card, allowed, stock_name_dict={
        "京能电力": "600578", "大唐发电": "601991",
    })
    assert not ok
    assert any(x.target == "601991" for x in v)
    assert any(x.target == "大唐发电" for x in v)

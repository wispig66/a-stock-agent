"""
theme_emergence_loop 校准报告工具。

跑法：
  uv run code/analyze_theme_loop.py                   # 全部历史
  uv run code/analyze_theme_loop.py --days 5          # 最近 N 天
  uv run code/analyze_theme_loop.py --date 2026-05-19 # 单日详情

ground truth：当日 ths_hot_reason 概念字段 split('+') 后频次 ≥3 的 tag 视为"真主线"。
首版纪律：T1 准确率目标 ≥60% / T2 ≥80%；延迟目标 ≤10min（首封 → T1 告警）。
"""

from __future__ import annotations
import argparse
import sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

from db import connect as db_connect  # noqa: E402

DB = ROOT / "data" / "daily.db"
GROUND_TRUTH_THRESHOLD = 3  # 当日某 tag 出现 ≥3 次 = 真主线


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=0,
                   help="最近 N 天（0=全部）")
    p.add_argument("--date", type=str, default=None,
                   help="单日详情，格式 YYYY-MM-DD")
    return p.parse_args()


def date_filter(days: int, single: str | None) -> str:
    if single:
        return f" AND trade_date='{single}' "
    if days > 0:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return f" AND trade_date >= '{cutoff}' "
    return ""


def load_emergence_log(filt: str) -> list[dict]:
    """读 theme_emergence_log。"""
    conn = db_connect(DB)
    try:
        cur = conn.execute(
            f"""SELECT id, detected_at, trade_date, concept_tag, signal_level,
                       signals_hit, cluster_count, first_leader, first_seal_time, ph_value
               FROM theme_emergence_log
               WHERE 1=1 {filt}
               ORDER BY detected_at""")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def load_ground_truth(filt: str) -> dict[str, Counter]:
    """{date: Counter({tag: freq})} — ths_hot_reason 当日概念串拆词频次。"""
    conn = db_connect(DB)
    try:
        cur = conn.execute(
            f"SELECT date, reason FROM ths_hot_reason WHERE 1=1 {filt.replace('trade_date', 'date')}")
        gt: dict[str, Counter] = defaultdict(Counter)
        for date, reason in cur.fetchall():
            for tag in (reason or "").split("+"):
                tag = tag.strip()
                if tag:
                    gt[date][tag] += 1
        return gt
    finally:
        conn.close()


def is_true_mainline(concept: str, date: str, gt: dict[str, Counter]) -> bool:
    """ground truth 命中：当日该 tag 频次 ≥ 阈值，或部分匹配（'存储芯片' 命中 'HBM 存储芯片'）"""
    counts = gt.get(date, Counter())
    if counts.get(concept, 0) >= GROUND_TRUTH_THRESHOLD:
        return True
    # 子串匹配
    total = sum(c for k, c in counts.items() if concept in k or k in concept)
    return total >= GROUND_TRUTH_THRESHOLD


def parse_first_seal_to_dt(date: str, hms: str) -> datetime | None:
    if not hms:
        return None
    try:
        return datetime.fromisoformat(f"{date}T{hms}")
    except ValueError:
        return None


def report(args):
    filt = date_filter(args.days, args.date)
    rows = load_emergence_log(filt)
    gt = load_ground_truth(filt)

    if not rows:
        print("⚠️ theme_emergence_log 无数据。daemon 还没跑过吗？")
        return

    # 总览
    t1 = [r for r in rows if r["signal_level"] == "T1"]
    t2 = [r for r in rows if r["signal_level"] == "T2"]
    dates = sorted({r["trade_date"] for r in rows})

    print(f"\n{'═' * 60}")
    print(f"theme_emergence_loop 校准报告 · {dates[0]} → {dates[-1]} ({len(dates)} 个交易日)")
    print(f"{'═' * 60}\n")

    # 1. 触发次数
    print(f"📊 触发统计")
    print(f"   T1 总数：{len(t1):3d}  · 平均 {len(t1)/len(dates):.1f}/天")
    print(f"   T2 总数：{len(t2):3d}  · 平均 {len(t2)/len(dates):.1f}/天")
    print(f"   T1→T2 升级率：{(len(t2)/len(t1)*100 if t1 else 0):.0f}%\n")

    # 2. 准确率
    t1_hit = sum(1 for r in t1 if is_true_mainline(r["concept_tag"], r["trade_date"], gt))
    t2_hit = sum(1 for r in t2 if is_true_mainline(r["concept_tag"], r["trade_date"], gt))
    t1_acc = t1_hit / len(t1) * 100 if t1 else 0
    t2_acc = t2_hit / len(t2) * 100 if t2 else 0
    print(f"🎯 准确率（vs ths_hot_reason 当日 ≥{GROUND_TRUTH_THRESHOLD} 次 tag）")
    print(f"   T1：{t1_hit}/{len(t1)} = {t1_acc:.1f}%  {'✅' if t1_acc >= 60 else '⚠️ 目标 ≥60%'}")
    print(f"   T2：{t2_hit}/{len(t2)} = {t2_acc:.1f}%  {'✅' if t2_acc >= 80 else '⚠️ 目标 ≥80%'}\n")

    # 3. 延迟（首封 → T1 告警）
    delays = []
    for r in t1:
        if r["first_seal_time"]:
            fs = parse_first_seal_to_dt(r["trade_date"], r["first_seal_time"])
            det = datetime.fromisoformat(r["detected_at"])
            if fs:
                delays.append((det - fs).total_seconds() / 60)
    if delays:
        delays.sort()
        avg = sum(delays) / len(delays)
        median = delays[len(delays) // 2]
        print(f"⏱️ T1 触发延迟（首封 → 告警，分钟）")
        print(f"   平均 {avg:.1f}min  中位 {median:.1f}min  最长 {max(delays):.1f}min")
        print(f"   {'✅' if avg <= 10 else '⚠️ 目标 ≤10min'}\n")

    # 4. 按题材分布
    by_concept = Counter(r["concept_tag"] for r in t1)
    print(f"🏷️ T1 题材分布 (top 10)")
    for tag, n in by_concept.most_common(10):
        hit_rate = sum(1 for r in t1 if r["concept_tag"] == tag
                       and is_true_mainline(tag, r["trade_date"], gt)) / n * 100
        print(f"   {tag:12s}  {n}次  命中率 {hit_rate:.0f}%")
    print()

    # 5. 单日详情（如果 --date）
    if args.date:
        print(f"📅 {args.date} 详细记录")
        for r in rows:
            level = r["signal_level"]
            t = r["detected_at"].split("T")[1] if "T" in r["detected_at"] else ""
            mark = "✅" if is_true_mainline(r["concept_tag"], r["trade_date"], gt) else "❌"
            print(f"   {t} [{level}] {mark} {r['concept_tag']:12s} "
                  f"cluster={r['cluster_count']} leader={r['first_leader']} "
                  f"@ {r['first_seal_time']}")
        print()

    # 6. 调参建议
    print(f"💡 调参建议")
    t1_per_day = len(t1) / len(dates)
    if t1_per_day > 20:
        print(f"   T1 频率 {t1_per_day:.1f}/天偏高，建议把 PH_LAMBDA 从 10 升到 20")
    elif t1_per_day < 3:
        print(f"   T1 频率 {t1_per_day:.1f}/天偏低，建议把 PH_LAMBDA 从 10 降到 5")
    else:
        print(f"   T1 频率 {t1_per_day:.1f}/天在合理区间 (3-20)，PH_LAMBDA 保持 10")

    if t1_acc < 60 and len(t1) >= 5:
        # 看常见假阳性 concept
        fp_concepts = Counter(r["concept_tag"] for r in t1
                              if not is_true_mainline(r["concept_tag"], r["trade_date"], gt))
        print(f"   T1 准确率不达标。常见假阳性题材：{dict(fp_concepts.most_common(5))}")
        print(f"   建议：检查白名单 keywords 是否过宽")

    # 7. 当日真主线 vs 系统识别（最后一天）
    last_date = dates[-1]
    real_mainlines = {k for k, v in gt.get(last_date, {}).items()
                      if v >= GROUND_TRUTH_THRESHOLD}
    detected = {r["concept_tag"] for r in rows if r["trade_date"] == last_date
                and r["signal_level"] in ("T1", "T2")}
    print(f"\n🔍 {last_date} 真主线 vs 系统识别")
    print(f"   真主线 (ths reason ≥{GROUND_TRUTH_THRESHOLD}): {sorted(real_mainlines)}")
    print(f"   系统识别: {sorted(detected)}")
    miss = real_mainlines - detected
    extra = detected - real_mainlines
    if miss:
        print(f"   🚨 漏识别: {sorted(miss)}")
    if extra:
        print(f"   ⚠️ 多识别: {sorted(extra)}")


if __name__ == "__main__":
    report(parse_args())

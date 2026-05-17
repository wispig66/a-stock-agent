#!/usr/bin/env python3
"""L7 stock-weekly Step 1 数据聚合 CLI。

输出 markdown fact pack 供 skill 消费。等同 stock-postmarket
的 fetch_postmarket.py 风格（stdout 即 fact pack）。

用法：
  uv run .claude/skills/stock-weekly/scripts/aggregate.py
  uv run .claude/skills/stock-weekly/scripts/aggregate.py --end-date 2026-05-17
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "code"))

from lib.weekly_pack import build_weekly_data_pack  # noqa: E402


def render_fact_pack(pack: dict) -> str:
    lines = [
        f"# 周复盘 fact pack · {pack['week_label']}",
        f"周一: {pack['monday']}  周五: {pack['friday']}  交易日: {pack['trading_days_in_week']}/5",
        "",
        "## 情绪曲线",
    ]
    for s in pack["sentiment_series"]:
        lines.append(
            f"- {s['date']} phase={s.get('phase')} 涨停={s.get('limit_up_count')} "
            f"跌停={s.get('limit_down_count')} 高度={s.get('max_consec')} "
            f"晋级={s.get('promotion_rate')} 炸板={s.get('blast_rate')}"
        )
    lines.append("")
    lines.append("## 周涨幅 Top 20")
    for g in pack["top_gainers"]:
        lines.append(f"- {g['code']}: {g['week_pct']:+.2f}% 成交 {g['amount']:.0f}")
    lines.append("")
    lines.append("## 同花顺题材热度（最近 200 条）")
    for r in pack["ths_hot_reasons"][:50]:
        lines.append(f"- {r['date']} {r['code']} {r.get('name','')} | {r.get('reason','')}")
    lines.append("")
    lines.append("## 龙虎榜席位 Top 15")
    for s in pack["lhb_seats"]:
        lines.append(f"- {s['seat_name']}: 上榜 {s['n']} 次 净 {s['net_sum']:.0f}")
    lines.append("")
    lines.append("## 本周个人交易")
    if not pack["weekly_trades"]:
        lines.append("（空仓周）")
    else:
        for t in pack["weekly_trades"]:
            lines.append(
                f"- {t['ts']} {t['side']} {t['code']} @ {t['price']} × {t['qty']} "
                f"reason={t.get('reason') or '-'}"
            )
    lines.append("")
    lines.append("## 周内异动日报文件")
    for p in pack["anomaly_files"]:
        lines.append(f"- {p}")
    lines.append("")
    lines.append("## raw JSON")
    lines.append("```json")
    lines.append(json.dumps(pack, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end-date", default=None,
                    help="YYYY-MM-DD; default = today")
    args = ap.parse_args()

    end = date.fromisoformat(args.end_date) if args.end_date else date.today()
    pack = build_weekly_data_pack(end_date=end)
    print(render_fact_pack(pack))
    return 0


if __name__ == "__main__":
    sys.exit(main())

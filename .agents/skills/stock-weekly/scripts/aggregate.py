#!/usr/bin/env python3
"""L7 stock-weekly Step 1 数据聚合 CLI。

输出 markdown fact pack 供 skill 消费。等同 stock-postmarket
的 fetch_postmarket.py 风格（stdout 即 fact pack）。

用法：
  uv run .agents/skills/stock-weekly/scripts/aggregate.py
  uv run .agents/skills/stock-weekly/scripts/aggregate.py --end-date 2026-05-17
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

from stock_codex.market.weekly_pack import build_weekly_data_pack  # noqa: E402

import sqlite3
from datetime import datetime as _dt

DB = ROOT / "data" / "daily.db"


def _stock_name(code: str) -> str:
    try:
        with sqlite3.connect(DB) as conn:
            row = conn.execute(
                "SELECT name FROM stock_basic WHERE code=?", (code,)
            ).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def build_allowed(pack: dict) -> dict:
    """聚合周复盘 fact pack 全部允许引用事实。"""
    codes: dict[str, str] = {}
    pct: dict[str, float] = {}
    concepts: list[str] = []

    # top_gainers: week_pct
    for g in pack.get("top_gainers") or []:
        code = str(g.get("code") or "")
        if not code or len(code) != 6:
            continue
        codes.setdefault(code, _stock_name(code))
        try:
            pct[code] = round(float(g.get("week_pct")), 2)
        except (TypeError, ValueError):
            pass

    # ths_hot_reasons: code/name + reason 拆 concepts
    for r in pack.get("ths_hot_reasons") or []:
        code = str(r.get("code") or "")
        name = str(r.get("name") or "")
        if code and len(code) == 6:
            codes[code] = name or codes.get(code, name)
        reason = str(r.get("reason", "") or "").strip()
        for t in (x.strip() for x in reason.split("+") if x.strip()):
            if t not in concepts:
                concepts.append(t)

    # weekly_trades: 用户本人交易
    for t in pack.get("weekly_trades") or []:
        code = str(t.get("code") or "")
        if code and len(code) == 6:
            codes.setdefault(code, _stock_name(code))

    # 周情绪 series（不直接进卡片数字，但允许引用）
    summary = {
        "week_label": pack.get("week_label"),
        "monday": str(pack.get("monday")),
        "friday": str(pack.get("friday")),
        "trading_days": pack.get("trading_days_in_week"),
        "date": str(pack.get("friday")),  # 用周五日期作 snapshot 日
    }
    series = pack.get("sentiment_series") or []
    if series:
        try:
            summary["max_consec_week"] = max(
                int(s.get("max_consec") or 0) for s in series
            )
            summary["max_limit_up_week"] = max(
                int(s.get("limit_up_count") or 0) for s in series
            )
        except Exception:
            pass

    return {
        "schema_version": "1",
        "skill": "stock-weekly",
        "snapshot_at": _dt.now().replace(microsecond=0).isoformat(),
        "codes": codes,
        "lianban": {},  # 周报不用单日连板，全周变化太大
        "pct": pct,  # 周涨跌幅，容差仍 ±0.5%
        "summary": summary,
        "concepts": concepts[:50],
        "news": [],
        "global_markets": {},
    }


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

    # ALLOWED 段
    allowed = build_allowed(pack)
    print("\n=== ALLOWED ===")
    print(json.dumps(allowed, ensure_ascii=False, indent=2))
    print("=== /ALLOWED ===")
    out_file = ROOT / "data" / "allowed_latest_stock-weekly.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(allowed, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

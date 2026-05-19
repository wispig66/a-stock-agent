"""stock-anomaly 卡片 fact pack。

读最近 N 分钟的 push_log[source=stock-anomaly] 推送 + 同花顺 reason tag，
构建 ALLOWED 段 + 结构化清单供 SKILL.md 用。

用法:
  uv run .claude/skills/stock-anomaly/scripts/build_fact_pack.py
  uv run .claude/skills/stock-anomaly/scripts/build_fact_pack.py --window-min 30
"""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DB = ROOT / "data" / "daily.db"
sys.path.insert(0, str(ROOT / "code"))

CODE_RE = re.compile(r"\b(\d{6})\b")


def fetch_anomaly_entries(window_min: int) -> list[dict]:
    """读最近 window_min 分钟内 source=stock-anomaly 的 push_log 条目。"""
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            "SELECT timestamp, text FROM push_log "
            "WHERE source='stock-anomaly' "
            "  AND datetime(timestamp) >= datetime('now', 'localtime', ?) "
            "ORDER BY id ASC",
            (f"-{window_min} minutes",),
        ).fetchall()
    out = []
    for ts, text in rows:
        out.append({"timestamp": ts, "text": text})
    return out


def fetch_ths_hot_latest() -> dict:
    """读 ths_hot_reason 最新一天的所有 reason + code/name。"""
    with sqlite3.connect(DB) as conn:
        latest = conn.execute(
            "SELECT MAX(date) FROM ths_hot_reason"
        ).fetchone()
        if not latest or not latest[0]:
            return {"date": None, "rows": []}
        date = latest[0]
        rows = conn.execute(
            "SELECT code, name, reason FROM ths_hot_reason WHERE date = ? ORDER BY change_pct DESC",
            (date,),
        ).fetchall()
    return {
        "date": date,
        "rows": [{"code": c, "name": n, "reason": r} for c, n, r in rows],
    }


def fetch_stock_basic_names(codes: set[str]) -> dict[str, str]:
    """从 stock_basic 查代码 → 名称。"""
    if not codes:
        return {}
    placeholders = ",".join("?" * len(codes))
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            f"SELECT code, name FROM stock_basic WHERE code IN ({placeholders})",
            tuple(codes),
        ).fetchall()
    return {c: n for c, n in rows}


def extract_codes_from_entries(entries: list[dict]) -> set[str]:
    codes = set()
    for e in entries:
        for m in CODE_RE.finditer(e.get("text", "")):
            codes.add(m.group(1))
    return codes


def build_allowed(entries: list[dict], ths_hot: dict, names: dict[str, str]) -> dict:
    codes: dict[str, str] = {}
    concepts: list[str] = []

    # 异动推送里的所有股票
    for code in extract_codes_from_entries(entries):
        codes[code] = names.get(code, "")

    # 同花顺热点里出现过的也允许（reason tag 交叉验证场景）
    for r in ths_hot.get("rows", []):
        code = r["code"]
        codes[code] = r["name"]
        reason = (r["reason"] or "").strip()
        for tag in (x.strip() for x in reason.split("+") if x.strip()):
            if tag not in concepts:
                concepts.append(tag)

    # 异动统计
    type_counts = {"火箭发射": 0, "封涨停": 0, "炸板": 0, "60日新高": 0}
    for e in entries:
        text = e.get("text", "")
        for k in type_counts:
            if k in text:
                type_counts[k] += 1
                break

    return {
        "schema_version": "1",
        "skill": "stock-anomaly",
        "snapshot_at": datetime.now().replace(microsecond=0).isoformat(),
        "codes": codes,
        "lianban": {},  # anomaly 不直接给连板（要 SKILL.md 时拉 zt_pool 时再加）
        "pct": {},
        "summary": {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "window_min": None,  # 调用方填
            "anomaly_count": len(entries),
            "type_counts": type_counts,
            "ths_hot_date": ths_hot.get("date"),
        },
        "concepts": concepts[:50],
        "news": [],  # anomaly skill 走 WebFetch（不在 fact pack 里），v2 搬
        "global_markets": {},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-min", type=int, default=30)
    args = ap.parse_args()

    entries = fetch_anomaly_entries(args.window_min)
    ths_hot = fetch_ths_hot_latest()
    codes = extract_codes_from_entries(entries)
    names = fetch_stock_basic_names(codes | {r["code"] for r in ths_hot.get("rows", [])})

    print(f"=== stock-anomaly fact pack · window={args.window_min}min ===\n")
    print(f"## 一、最近 {args.window_min} 分钟 anomaly_loop 推送（{len(entries)} 条）\n")
    if not entries:
        print("- 无（anomaly_loop 可能未启动或交易时段外）")
    else:
        for e in entries[-30:]:  # 最近 30 条
            print(f"- {e['timestamp']}  {e['text'].splitlines()[0][:120]}")

    print(f"\n## 二、ths_hot_reason 最新（{ths_hot['date']}，{len(ths_hot['rows'])} 只）\n")
    for r in ths_hot["rows"][:30]:
        print(f"- {r['code']} {r['name']}  题材：{r['reason']}")

    allowed = build_allowed(entries, ths_hot, names)
    allowed["summary"]["window_min"] = args.window_min

    print("\n=== ALLOWED ===")
    print(json.dumps(allowed, ensure_ascii=False, indent=2))
    print("=== /ALLOWED ===")

    out_file = ROOT / "data" / "allowed_latest_stock-anomaly.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(allowed, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

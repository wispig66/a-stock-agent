#!/usr/bin/env python3
"""
review.py — 解析 push_log 中的盘前观察池推送，自动抽取股票代码/买点/止损/派别/题材，
可选拉今日行情对照命中状态，输出 JSON 或 markdown 复盘表 + 命中率统计。

用法:
  python review.py parse                       解析今日最新 premarket 推送，输出 JSON
  python review.py parse --date 2026-05-13     指定日期
  python review.py parse --format markdown     一行一只的简表
  python review.py review                      parse + akshare 实时行情 + markdown 复盘表
  python review.py review --format json        review 结果 JSON
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from db import connect as db_connect

DB = Path(__file__).resolve().parent.parent / "data" / "daily.db"

REVIEW_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    faction TEXT,
    genre TEXT,
    buy_point TEXT,
    stop_loss TEXT,
    high REAL, low REAL, close REAL, pct REAL,
    status TEXT,
    UNIQUE(review_date, code)
);
"""

FACTION_RE = re.compile(r"【\s*派别\s*([A-Z])\s*[·・]\s*([^】]+)】")
STOCK_HEAD_RE = re.compile(r"^\s*(\d+)\.\s*(\d{6})\s+([^\s\[\(（【]+)")
BUY_PRICE_RE = re.compile(r"买点[：:][^\n]*?¥\s*([\d.]+)")
BUY_MA_RE = re.compile(r"买点[：:][^\n]*?5\s*日线")
STOP_PRICE_RE = re.compile(r"止损[：:][^\n]*?¥\s*([\d.]+)")
STOP_MA_RE = re.compile(r"(?:止损|跌破)[^\n]*?5\s*日线")
POS_RE = re.compile(r"首仓\s*[≤<]?\s*(\d+)\s*%")
GENRE_RE = re.compile(r"\[([^\]]+)\]")

SECTION_BREAK_PREFIX = ("📰", "⚠️", "---", "🎯", "🔥", "🌡️", "📋", "本系统", "fact pack")


def parse_premarket(text: str) -> list[dict]:
    lines = text.splitlines()
    entries: list[dict] = []
    cur_faction = cur_faction_name = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = FACTION_RE.search(line)
        if m:
            cur_faction, cur_faction_name = m.group(1), m.group(2).strip()
            i += 1
            continue
        m = STOCK_HEAD_RE.match(line)
        if m and cur_faction:
            seq, code, name = m.groups()
            block = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if STOCK_HEAD_RE.match(nxt) or FACTION_RE.search(nxt):
                    break
                stripped = nxt.lstrip()
                if any(stripped.startswith(p) for p in SECTION_BREAK_PREFIX):
                    break
                block.append(nxt)
                j += 1
            blk = "\n".join(block)

            if (bp := BUY_PRICE_RE.search(blk)):
                buy = float(bp.group(1))
            elif BUY_MA_RE.search(blk):
                buy = "MA5"
            else:
                buy = None

            if (sp := STOP_PRICE_RE.search(blk)):
                stop = float(sp.group(1))
            elif STOP_MA_RE.search(blk):
                stop = "MA5"
            else:
                stop = None

            genre = GENRE_RE.search(blk)
            pos = POS_RE.search(blk)

            entries.append({
                "seq": int(seq),
                "code": code,
                "name": name,
                "faction": cur_faction,
                "faction_name": cur_faction_name,
                "genre": genre.group(1) if genre else None,
                "buy_point": buy,
                "stop_loss": stop,
                "position_max_pct": int(pos.group(1)) if pos else None,
            })
            i = j
            continue
        i += 1
    return entries


def fetch_premarket(target_date: str | None) -> tuple[int, str] | None:
    conn = db_connect(DB)
    if target_date:
        row = conn.execute(
            "SELECT msg_id, text FROM push_log "
            "WHERE source='stock-premarket' AND date(timestamp)=? "
            "ORDER BY id DESC LIMIT 1",
            (target_date,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT msg_id, text FROM push_log "
            "WHERE source='stock-premarket' "
            "AND date(timestamp)=date('now','localtime') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    conn.close()
    return row


def review_today(entries: list[dict]) -> list[dict]:
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    codes = [e["code"] for e in entries]
    sub = df[df["代码"].isin(codes)].set_index("代码")
    for e in entries:
        code = e["code"]
        if code not in sub.index:
            e["status"] = "无数据"
            continue
        row = sub.loc[code]
        e["close"] = float(row["最新价"])
        e["high"] = float(row["最高"])
        e["low"] = float(row["最低"])
        e["pct"] = float(row["涨跌幅"])
        try:
            e["turnover"] = float(row["换手率"])
        except (KeyError, ValueError):
            e["turnover"] = None

        buy, stop = e["buy_point"], e["stop_loss"]
        if isinstance(buy, (int, float)):
            triggered = e["high"] >= buy
            closed_red = e["close"] >= buy
            if isinstance(stop, (int, float)) and e["low"] <= stop:
                e["status"] = "💥 跌破止损"
            elif triggered and closed_red:
                e["status"] = "✅ 触发+收红"
            elif triggered:
                e["status"] = "⚠️ 触发+假突破"
            else:
                e["status"] = "❌ 未触发"
        else:
            if e["pct"] >= 5:
                e["status"] = "✅ 强势(MA5派)"
            elif e["pct"] >= 0:
                e["status"] = "⚪ 平盘(MA5派)"
            else:
                e["status"] = "❌ 收绿(MA5派)"
    return entries


def persist_stats(reviewed: list[dict], review_date: str) -> int:
    with db_connect(DB) as conn:
        conn.execute(REVIEW_STATS_SCHEMA)
        n = 0
        for e in reviewed:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO review_stats
                       (review_date, code, name, faction, genre, buy_point, stop_loss,
                        high, low, close, pct, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (review_date, e["code"], e["name"], e["faction"], e.get("genre"),
                     str(e["buy_point"]) if e["buy_point"] is not None else None,
                     str(e["stop_loss"]) if e["stop_loss"] is not None else None,
                     e.get("high"), e.get("low"), e.get("close"), e.get("pct"),
                     e.get("status")),
                )
                n += 1
            except Exception as ex:
                print(f"[review] persist {e['code']} 失败: {ex}", file=sys.stderr)
    return n


def render_markdown(reviewed: list[dict]) -> str:
    out = [
        "| # | 代码 | 名称 | 派 | 买点 | 止损 | 今收 | 涨跌% | 状态 |",
        "|---|------|------|----|------|------|------|-------|------|",
    ]
    fixed = trig = red = fb = sl = 0
    for e in reviewed:
        bp = e["buy_point"]
        sp = e["stop_loss"]
        buy_s = f"{bp:.2f}" if isinstance(bp, (int, float)) else (bp or "-")
        stop_s = f"{sp:.2f}" if isinstance(sp, (int, float)) else (sp or "-")
        close_s = f"{e['close']:.2f}" if "close" in e else "-"
        pct_s = f"{e['pct']:+.2f}" if "pct" in e else "-"
        out.append(
            f"| {e['seq']} | {e['code']} | {e['name']} | {e['faction']} | "
            f"{buy_s} | {stop_s} | {close_s} | {pct_s} | {e.get('status','-')} |"
        )
        if isinstance(bp, (int, float)):
            fixed += 1
            st = e.get("status", "")
            if "触发" in st:
                trig += 1
            if "收红" in st:
                red += 1
            if "假突破" in st:
                fb += 1
            if "止损" in st:
                sl += 1
    if fixed:
        out += [
            "",
            f"**命中率统计**（定价候选 {fixed} 只）",
            f"- 触发率：{trig}/{fixed} = {trig/fixed:.0%}",
            f"- 收红率：{red}/{fixed} = {red/fixed:.0%}",
            f"- 假突破率：{fb}/{fixed} = {fb/fixed:.0%}",
            f"- 止损命中率：{sl}/{fixed} = {sl/fixed:.0%}",
        ]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["parse", "review"])
    ap.add_argument("--date", help="YYYY-MM-DD，默认今日")
    ap.add_argument("--format", choices=["json", "markdown"])
    ap.add_argument("--no-persist", action="store_true", help="review 子命令默认入 review_stats 表")
    args = ap.parse_args()

    row = fetch_premarket(args.date)
    if not row:
        sys.exit(f"[review] 未找到 {args.date or '今日'} 的 stock-premarket 推送")
    _, text = row
    entries = parse_premarket(text)
    if not entries:
        sys.exit("[review] 解析出 0 只股票，检查推送格式或正则")

    if args.cmd == "parse":
        fmt = args.format or "json"
        if fmt == "json":
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            for e in entries:
                print(f"{e['code']} {e['name']} [{e['faction']}] "
                      f"buy={e['buy_point']} stop={e['stop_loss']} pos={e['position_max_pct']}")
    else:
        reviewed = review_today(entries)
        if not args.no_persist:
            from datetime import date
            rd = args.date or date.today().isoformat()
            n = persist_stats(reviewed, rd)
            print(f"[review] 写入 review_stats {n} 行（{rd}）", file=sys.stderr)
        fmt = args.format or "markdown"
        if fmt == "json":
            print(json.dumps(reviewed, ensure_ascii=False, indent=2, default=str))
        else:
            print(render_markdown(reviewed))


if __name__ == "__main__":
    main()

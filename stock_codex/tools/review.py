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
from datetime import date
from pathlib import Path

import pandas as pd

from stock_codex.infra.db import connect as db_connect
from stock_codex.domain.decision import load_tickets
from stock_codex.paths import DATA_DIR, DB_FILE as DB

POSTMARKET_CACHE_DIR = DATA_DIR / "postmarket_cache"
WATCH_RAW_DIR = DATA_DIR / "watch_raw"
ALLOWED_POSTMARKET = DATA_DIR / "allowed_latest_stock-postmarket.json"
SPOT_COLUMNS = ["代码", "名称", "最新价", "涨跌幅", "最高", "最低", "今开", "换手率", "成交额"]

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


class ReviewDataUnavailable(RuntimeError):
    pass


def _iso_to_ymd(value: str) -> str:
    return value.replace("-", "")


def _clean_code(value) -> str:
    return str(value).strip().zfill(6)


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_value(value) -> bool:
    if value is None:
        return False
    try:
        return not pd.isna(value)
    except TypeError:
        return True


def _read_split_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get("data") or {}
    df = pd.DataFrame(data.get("data") or [], columns=data.get("columns") or [])
    if not df.empty:
        df.attrs["snapshot_at"] = payload.get("cached_at")
    return _normalize_spot_df(df)


def _normalize_spot_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "代码" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["代码"] = out["代码"].map(_clean_code)
    cols = [c for c in SPOT_COLUMNS if c in out.columns]
    return out[cols].reset_index(drop=True)


def _load_spot_cache(review_date: str, codes: list[str]) -> pd.DataFrame:
    path = POSTMARKET_CACHE_DIR / f"{_iso_to_ymd(review_date)}_spot.json"
    df = _read_split_cache(path)
    if df.empty:
        return df
    df = df[df["代码"].isin(codes)].copy()
    df.attrs["review_source"] = f"spot-cache:{path.name}"
    return df


def _load_watch_raw(review_date: str, codes: list[str]) -> pd.DataFrame:
    path = WATCH_RAW_DIR / f"{_iso_to_ymd(review_date)}.jsonl"
    if not path.exists():
        return pd.DataFrame()
    wanted = set(codes)
    agg: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        code = _clean_code(rec.get("代码"))
        if code not in wanted:
            continue
        row = agg.setdefault(code, {"代码": code, "名称": rec.get("名称")})
        high = _num(rec.get("最高"))
        low = _num(rec.get("最低"))
        if high is not None:
            row["最高"] = max(_num(row.get("最高")) or high, high)
        if low is not None:
            prev_low = _num(row.get("最低"))
            row["最低"] = min(prev_low if prev_low is not None else low, low)
        if str(rec.get("round_ts") or "") >= str(row.get("_round_ts") or ""):
            row["_round_ts"] = rec.get("round_ts")
            row["最新价"] = _num(rec.get("最新价"))
            row["涨跌幅"] = _num(rec.get("涨跌幅"))
            row["今开"] = _num(rec.get("今开"))
            row["换手率"] = _num(rec.get("换手率"))
            row["成交额"] = _num(rec.get("成交额"))
    rows = [{k: v for k, v in row.items() if k != "_round_ts"} for row in agg.values()]
    df = _normalize_spot_df(pd.DataFrame(rows))
    if not df.empty:
        df.attrs["review_source"] = f"watch-raw:{path.name}"
    return df


def _load_structural_or_allowed(review_date: str, codes: list[str]) -> pd.DataFrame:
    wanted = set(codes)
    rows: dict[str, dict] = {}
    ymd = _iso_to_ymd(review_date)
    for cache_name in ("zt", "zd", "zb", "qs"):
        df = _read_split_cache(POSTMARKET_CACHE_DIR / f"{ymd}_{cache_name}.json")
        if df.empty:
            continue
        for row in df.to_dict("records"):
            code = row.get("代码")
            if code not in wanted:
                continue
            dest = rows.setdefault(code, {"代码": code})
            for field in ("名称", "最新价", "涨跌幅", "换手率", "成交额"):
                if not _has_value(dest.get(field)) and _has_value(row.get(field)):
                    dest[field] = row.get(field)
    if ALLOWED_POSTMARKET.exists():
        allowed = json.loads(ALLOWED_POSTMARKET.read_text(encoding="utf-8"))
        if (allowed.get("summary") or {}).get("date") == review_date:
            for code in wanted:
                name = (allowed.get("codes") or {}).get(code)
                pct = (allowed.get("pct") or {}).get(code)
                if name is None and pct is None:
                    continue
                dest = rows.setdefault(code, {"代码": code})
                if name and not _has_value(dest.get("名称")):
                    dest["名称"] = name
                if pct is not None and not _has_value(dest.get("涨跌幅")):
                    dest["涨跌幅"] = pct
    df = _normalize_spot_df(pd.DataFrame(rows.values()))
    if not df.empty:
        df.attrs["review_source"] = "postmarket-structural-partial"
    return df


def _load_akshare_spot(codes: list[str]) -> pd.DataFrame:
    import akshare as ak
    df = _normalize_spot_df(ak.stock_zh_a_spot_em())
    if df.empty:
        return df
    df = df[df["代码"].isin(codes)].copy()
    df.attrs["review_source"] = "akshare-live"
    return df


def _merge_spot_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    merged: dict[str, dict] = {}
    sources: list[str] = []
    for df in frames:
        if df.empty:
            continue
        source = df.attrs.get("review_source")
        if source and source not in sources:
            sources.append(str(source))
        for row in df.to_dict("records"):
            code = row.get("代码")
            if not code:
                continue
            dest = merged.setdefault(code, {"代码": code})
            for field, value in row.items():
                if not _has_value(dest.get(field)) and _has_value(value):
                    dest[field] = value
    out = _normalize_spot_df(pd.DataFrame(merged.values()))
    out.attrs["review_source"] = " + ".join(sources)
    return out


def _has_full_coverage(df: pd.DataFrame, codes: list[str]) -> bool:
    if df.empty:
        return False
    by_code = df.set_index("代码")
    for code in codes:
        if code not in by_code.index:
            return False
        row = by_code.loc[code]
        for field in ("最新价", "涨跌幅", "最高", "最低"):
            if field not in row or not _has_value(row[field]):
                return False
    return True


def _has_any_coverage(df: pd.DataFrame, codes: list[str]) -> bool:
    if df.empty:
        return False
    by_code = df.set_index("代码")
    useful_fields = ("名称", "最新价", "涨跌幅", "最高", "最低")
    for code in codes:
        if code not in by_code.index:
            return False
        row = by_code.loc[code]
        if not any(field in row and _has_value(row[field]) for field in useful_fields):
            return False
    return True


def load_decision_review_spot(review_date: str, codes: list[str], source: str = "auto") -> pd.DataFrame:
    codes = [_clean_code(code) for code in codes]
    loaders = {
        "spot-cache": lambda: _load_spot_cache(review_date, codes),
        "watch-raw": lambda: _load_watch_raw(review_date, codes),
        "postmarket-partial": lambda: _load_structural_or_allowed(review_date, codes),
        "akshare": lambda: _load_akshare_spot(codes),
    }
    order = ["spot-cache", "watch-raw", "postmarket-partial", "akshare"] if source == "auto" else [source]
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for name in order:
        try:
            df = loaders[name]()
            if not df.empty:
                frames.append(df)
                merged = _merge_spot_frames(frames)
                if source != "auto" or _has_full_coverage(merged, codes):
                    return merged
                if name == "postmarket-partial" and _has_any_coverage(merged, codes):
                    return merged
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
    if frames:
        return _merge_spot_frames(frames)
    detail = "; ".join(errors) if errors else "no local cache matched"
    raise ReviewDataUnavailable(f"decision-review 行情不可用（source={source}; {detail}）")


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


def review_decision_tickets(tickets: list[dict], spot_df) -> list[dict]:
    """Score decision_tickets against a spot dataframe without reparsing card text."""
    if spot_df is None or spot_df.empty:
        for t in tickets:
            t["status"] = "无行情数据"
        return tickets

    sub = spot_df[spot_df["代码"].isin([t["code"] for t in tickets])].set_index("代码")
    for t in tickets:
        code = t["code"]
        if code not in sub.index:
            t["status"] = "无数据"
            continue
        row = sub.loc[code]
        high = _num(row.get("最高"))
        low = _num(row.get("最低"))
        close = _num(row.get("最新价"))
        pct = _num(row.get("涨跌幅"))
        if high is not None:
            t["high"] = high
        if low is not None:
            t["low"] = low
        if close is not None:
            t["close"] = close
        if pct is not None:
            t["pct"] = pct

        lane = t.get("lane")
        stop = t.get("stop_price")
        entry_low = t.get("entry_low")
        entry_high = t.get("entry_high")

        if lane == "ban":
            t["status"] = "🚫 禁买后走强" if pct is not None and pct >= 5 else "✅ 禁买规避/无强信号"
        elif high is None or low is None or close is None:
            t["status"] = "行情不完整，无法判定触发"
        elif stop is not None and low <= float(stop):
            t["status"] = "💥 跌破止损/失效"
        elif lane in {"main", "backup", "trend"}:
            if entry_high is None:
                t["status"] = "不可执行：缺少买点"
                continue
            trigger = entry_high is not None and high >= float(entry_high)
            red = entry_high is not None and close >= float(entry_high)
            if trigger and red:
                if lane == "main":
                    t["status"] = "✅ 主攻触发+收红"
                elif lane == "trend":
                    t["status"] = "✅ 趋势触发+收红"
                else:
                    t["status"] = "✅ 备选触发+收红"
            elif trigger:
                t["status"] = "⚠️ 触发+假突破"
            else:
                t["status"] = "❌ 未触发"
        elif lane == "ambush":
            if entry_low is None or entry_high is None:
                t["status"] = "不可执行：缺少低吸区"
                continue
            touched = (
                entry_low is not None
                and entry_high is not None
                and low <= float(entry_high)
                and high >= float(entry_low)
            )
            t["status"] = "🟡 潜伏触达低吸区" if touched else "⏳ 潜伏未到低吸区"
        else:
            t["status"] = "未分类"
    return tickets


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


def render_decision_markdown(reviewed: list[dict], source_note: str | None = None) -> str:
    out = []
    if source_note:
        out += [f"数据源：{source_note}", ""]
    out += [
        "| lane | 代码 | 名称 | 派 | 区间/触发 | 今收 | 涨跌% | 状态 |",
        "|---|------|------|----|------|------|-------|------|",
    ]
    for t in reviewed:
        low = t.get("entry_low")
        high = t.get("entry_high")
        zone = "-"
        if low is not None and high is not None:
            zone = f"{low:.2f}-{high:.2f}"
        elif high is not None:
            zone = f">={high:.2f}"
        close_s = f"{t['close']:.2f}" if "close" in t else "-"
        pct_s = f"{t['pct']:+.2f}" if "pct" in t else "-"
        out.append(
            f"| {t.get('lane')} | {t['code']} | {t['name']} | {t.get('faction') or '-'} | "
            f"{zone} | {close_s} | {pct_s} | {t.get('status','-')} |"
        )

    main = [t for t in reviewed if t.get("lane") == "main"]
    ambush = [t for t in reviewed if t.get("lane") == "ambush"]
    bans = [t for t in reviewed if t.get("lane") == "ban"]
    out += [
        "",
        "**决策评分**",
        f"- 主攻：{main[0].get('status') if main else '今日无主攻'}",
        f"- 潜伏：{sum('低吸区' in t.get('status','') for t in ambush)}/{len(ambush)} 触达低吸区" if ambush else "- 潜伏：无",
        f"- 禁买：{sum('禁买后走强' in t.get('status','') for t in bans)}/{len(bans)} 禁买后走强" if bans else "- 禁买：无",
    ]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["parse", "review", "decision-review"])
    ap.add_argument("--date", help="YYYY-MM-DD，默认今日")
    ap.add_argument("--format", choices=["json", "markdown"])
    ap.add_argument("--source", choices=["auto", "spot-cache", "watch-raw", "postmarket-partial", "akshare"],
                    default="auto", help="decision-review 行情来源，默认 auto 多级降级")
    ap.add_argument("--no-persist", action="store_true", help="review 子命令默认入 review_stats 表")
    args = ap.parse_args()

    if args.cmd == "decision-review":
        rd = args.date or date.today().isoformat()
        tickets = load_tickets(DB, rd)
        if not tickets:
            sys.exit(f"[review] 未找到 {rd} 的 decision_tickets")
        try:
            df = load_decision_review_spot(rd, [t["code"] for t in tickets], args.source)
        except ReviewDataUnavailable as exc:
            sys.exit(f"[review] {exc}")
        reviewed = review_decision_tickets(tickets, df)
        fmt = args.format or "markdown"
        if fmt == "json":
            print(json.dumps(reviewed, ensure_ascii=False, indent=2, default=str))
        else:
            print(render_decision_markdown(reviewed, df.attrs.get("review_source")))
        return

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

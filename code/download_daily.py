"""
Week 1 最小验收脚本：拉全市场最近 N 天日线，写入 SQLite。

注意：当前出口 IP 在东财风控黑名单，efinance/akshare 东财源不可用。
本脚本用新浪接口（akshare.stock_zh_a_daily）。

用法：
    uv run code/download_daily.py            # 默认 30 天 + 全市场
    uv run code/download_daily.py 30 100     # 30 天 + 前 100 只（冒烟测试）
"""

from __future__ import annotations
import sys
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import akshare as ak

from db import connect as db_connect

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "daily.db"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "download_daily.log", "a") as f:
        f.write(line + "\n")


def code_to_sina(code: str) -> str:
    """A 股代码 -> 新浪 symbol (sh/sz/bj 前缀)。"""
    if code.startswith(("60", "68", "9")):
        return f"sh{code}"
    if code.startswith(("00", "30", "20")):
        return f"sz{code}"
    if code.startswith(("8", "43", "92")):
        return f"bj{code}"
    return f"sh{code}"  # 兜底


def list_all_a_codes() -> list[tuple[str, str]]:
    """全 A 代码 + 名称。剔除 ST 与新股（上市不足 60 天，新浪表里无上市日期，仅按命名过滤 ST）。"""
    df = ak.stock_info_a_code_name()
    df = df[~df["name"].str.contains("ST", na=False)]
    df = df[~df["name"].str.contains("退", na=False)]
    pairs = list(zip(df["code"], df["name"]))
    log(f"候选股票 {len(pairs)} 只")
    return pairs


def fetch_one(code: str, days: int) -> pd.DataFrame | None:
    """单只票 N 天日线（新浪，前复权）。"""
    end = datetime.now().strftime("%Y%m%d")
    beg = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
    sym = code_to_sina(code)
    try:
        df = ak.stock_zh_a_daily(symbol=sym, start_date=beg, end_date=end, adjust="qfq")
        if df is None or df.empty:
            return None
        df = df.tail(days).copy()
        df["code"] = code
        df["date"] = df["date"].astype(str)
        df["pct_chg"] = (df["close"].pct_change() * 100).round(2)
        return df[["code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]] \
            .rename(columns={"volume": "vol"})
    except Exception as e:
        log(f"  {code} 失败: {type(e).__name__}: {str(e)[:80]}")
        return None


def upsert_kline(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    rows = df.to_dict("records")
    cur = conn.executemany(
        """INSERT OR REPLACE INTO daily_kline
           (code, date, open, high, low, close, vol, amount, pct_chg)
           VALUES (:code, :date, :open, :high, :low, :close, :vol, :amount, :pct_chg)""",
        rows,
    )
    conn.commit()
    return cur.rowcount


def main(days: int = 30, limit: int | None = None) -> None:
    log(f"开始下载日线 days={days} limit={limit}")
    t0 = time.time()
    pairs = list_all_a_codes()
    if limit:
        pairs = pairs[:limit]

    conn = db_connect(DB)
    total_rows = 0
    failed: list[str] = []

    for i, (code, _) in enumerate(pairs, 1):
        df = fetch_one(code, days)
        if df is None or df.empty:
            failed.append(code)
            continue
        total_rows += upsert_kline(conn, df)

        if i % 100 == 0:
            elapsed = time.time() - t0
            eta = elapsed / i * (len(pairs) - i)
            log(f"  进度 {i}/{len(pairs)}  累计 {total_rows} 行  ETA {eta/60:.1f}min")

        time.sleep(0.1)  # 新浪反爬保守 100ms

    conn.close()
    log(f"完成。写入 {total_rows} 行，失败 {len(failed)} 只，总耗时 {time.time()-t0:.0f}s")
    if failed:
        log(f"失败代码（前 20）：{failed[:20]}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    main(days=days, limit=limit)

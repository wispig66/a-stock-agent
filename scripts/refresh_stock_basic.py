"""每日 17:00 刷新 stock_basic 表（代码→名称/板块/上市日/ST 标志）。

数据源：东财 `clist.dfcf` 全市场列表。失败重试 3 次，仍失败抛错让 launchd 记错；
不写入半成品。

用法：uv run scripts/refresh_stock_basic.py
"""
from __future__ import annotations
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from db import connect  # noqa: E402

DB = ROOT / "data" / "daily.db"

EM_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn=1&pz=10000&po=1&np=1&fltt=2&invt=2"
    "&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    "&fields=f12,f14,f26"
)


def infer_board(code: str) -> str:
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("8", "4")) and len(code) == 6:
        return "bse"
    return "main"


def fetch_all_stock_basic() -> list[dict]:
    """拉全市场。f12=code, f14=name, f26=上市日(yyyymmdd int)。"""
    for attempt in range(3):
        try:
            r = requests.get(EM_URL, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json().get("data") or {}
            diff = data.get("diff") or []
            out = []
            for row in diff:
                code = str(row.get("f12") or "").zfill(6)
                if not code or len(code) != 6:
                    continue
                name = (row.get("f14") or "").strip()
                list_raw = row.get("f26")
                if isinstance(list_raw, int) and list_raw > 19900101:
                    list_date = f"{str(list_raw)[:4]}-{str(list_raw)[4:6]}-{str(list_raw)[6:8]}"
                else:
                    list_date = None
                is_st = 1 if ("ST" in name or "*ST" in name) else 0
                out.append({
                    "code": code, "name": name, "board": infer_board(code),
                    "list_date": list_date, "is_st": is_st,
                })
            if out:
                return out
        except Exception as e:
            print(f"[refresh_stock_basic] attempt {attempt + 1} 失败: {e}",
                  file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError("stock_basic 全市场拉取连续 3 次失败")


def main() -> None:
    rows = fetch_all_stock_basic()
    today = date.today().isoformat()
    with connect(DB) as conn:
        conn.executemany(
            """INSERT INTO stock_basic(code,name,board,list_date,is_st,updated_at)
               VALUES(:code,:name,:board,:list_date,:is_st,:updated_at)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, board=excluded.board,
                 list_date=excluded.list_date, is_st=excluded.is_st,
                 updated_at=excluded.updated_at""",
            [{**r, "updated_at": today} for r in rows],
        )
        conn.commit()
    print(f"[refresh_stock_basic] upsert {len(rows)} rows")


if __name__ == "__main__":
    main()

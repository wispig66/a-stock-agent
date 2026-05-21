"""每日 17:00 刷新 stock_basic 表（代码→名称/板块/ST 标志）。

数据源：新浪 Market_Center.getHQNodeData（项目出口 IP 在东财风控黑名单，沿用既有做法）。
按 node=sh_a + sz_a 分页拉全市场。失败重试 3 次，仍失败抛错让 launchd 记错。

list_date 当前来源不提供（Sina 此接口无字段），统一写 NULL；未来若需要再接 ths/em。

用法：uv run scripts/refresh_stock_basic.py
"""
from __future__ import annotations
import sys
import time
from datetime import date

import requests

from stock_codex.infra.db import connect  # noqa: E402
from stock_codex.paths import DB_FILE as DB

SINA_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
PAGE_SIZE = 100


def infer_board(code: str) -> str:
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("8", "4")) and len(code) == 6:
        return "bse"
    return "main"


def _fetch_node(node: str) -> list[dict]:
    """分页拉一个 Sina node 的全部股票。"""
    out: list[dict] = []
    for page in range(1, 100):
        params = {"node": node, "num": PAGE_SIZE, "page": page}
        r = requests.get(SINA_URL, params=params, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rows = r.json() or []
        if not rows:
            break
        for row in rows:
            code = str(row.get("code") or "").zfill(6)
            if len(code) != 6:
                continue
            name = (row.get("name") or "").strip()
            out.append({
                "code": code, "name": name, "board": infer_board(code),
                "list_date": None,
                "is_st": 1 if ("ST" in name or "*ST" in name) else 0,
            })
        if len(rows) < PAGE_SIZE:
            break
    return out


def fetch_all_stock_basic() -> list[dict]:
    """合并 sh_a + sz_a；按 code 去重（防止 Sina 节点重叠）。"""
    for attempt in range(3):
        try:
            merged: dict[str, dict] = {}
            for node in ("sh_a", "sz_a"):
                for row in _fetch_node(node):
                    merged.setdefault(row["code"], row)
            if merged:
                return list(merged.values())
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

"""单股查询数据层。本模块包含两类函数：

1. 不联网（本任务实现）：parse_input / board_of / is_st / is_suspended_today
   / lookup_by_name
2. 联网拉数据（Task 4 后续补充）：fetch_kline / fetch_realtime /
   fetch_concept_strength / fetch_money_flow / fetch_recent_news

DB 默认指向 data/daily.db；测试用 monkeypatch 替换。
"""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path
from typing import Optional

from db import connect

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "daily.db"

_CODE_RE = re.compile(r"^\d{6}$")
_PREFIX_RE = re.compile(r"^(?:sh|sz|bj)", re.IGNORECASE)
_CHINESE_RE = re.compile(r"[㐀-䶿一-鿿]")


def parse_input(text: str) -> tuple[str, str]:
    """返回 (kind, value)。kind ∈ {"code","name","unknown"}。

    规则：去空白、去 $/# 前缀、去 SH/SZ/BJ 前缀；6 位纯数字→code；
    含中文→name（由 lookup_by_name 兜底"未找到"）；其它→unknown。
    """
    s = text.strip().lstrip("$#")
    s = _PREFIX_RE.sub("", s).strip()
    if _CODE_RE.match(s):
        return ("code", s)
    if _CHINESE_RE.search(s):
        return ("name", s)
    return ("unknown", s)


def board_of(code: str) -> Optional[str]:
    """返回股票所属板块（main/chinext/star/bse），未找到返回 None。"""
    with connect(DB) as conn:
        row = conn.execute(
            "SELECT board FROM stock_basic WHERE code = ?", (code,)
        ).fetchone()
    return row[0] if row else None


def is_st(code: str) -> bool:
    """返回该股是否为 ST/退市整理股。未在库中→False（保守）。"""
    with connect(DB) as conn:
        row = conn.execute(
            "SELECT is_st FROM stock_basic WHERE code = ?", (code,)
        ).fetchone()
    return bool(row and row[0])


def is_suspended_today(code: str, today: Optional[str] = None) -> bool:
    """无当日 daily_kline 记录 → 视为停牌（保守判定）。

    today 是 ISO yyyy-mm-dd；默认取当前日期。盘前/盘中调用时当日 kline 通常缺失，
    上层应用应只在盘后或确认数据已写入后才依赖此函数。
    """
    today = today or date.today().isoformat()
    with connect(DB) as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_kline WHERE code = ? AND date = ?",
            (code, today),
        ).fetchone()
    return row is None


def lookup_by_name(needle: str) -> list[tuple[str, str]]:
    """精确包含匹配（substring）。返回 [(code, name), ...]。"""
    with connect(DB) as conn:
        rows = conn.execute(
            "SELECT code, name FROM stock_basic WHERE name LIKE ?",
            (f"%{needle}%",),
        ).fetchall()
    return [(c, n) for c, n in rows]


# ============================================================
# 联网数据拉取（失败抛异常，调用方降级处理；fetch_recent_news 例外，失败返回 []）
# ============================================================
import requests
import pandas as pd


_UA = {"User-Agent": "Mozilla/5.0"}


def _sina_prefix(code: str) -> str:
    return "sh" + code if code.startswith(("5", "6", "9")) else "sz" + code


def fetch_realtime(code: str) -> dict:
    """新浪 hq.sinajs.cn 实时盘口。"""
    url = f"https://hq.sinajs.cn/list={_sina_prefix(code)}"
    r = requests.get(url, timeout=8,
                     headers={**_UA, "Referer": "https://finance.sina.com.cn"})
    r.raise_for_status()
    body = r.text.split('"')[1]
    parts = body.split(",")
    return {
        "name": parts[0], "open": float(parts[1]), "pre_close": float(parts[2]),
        "close": float(parts[3]), "high": float(parts[4]), "low": float(parts[5]),
        "vol": float(parts[8] or 0), "amount": float(parts[9] or 0),
        "date": parts[30] if len(parts) > 30 else "",
        "time": parts[31] if len(parts) > 31 else "",
    }


def fetch_kline(code: str, days: int = 60) -> pd.DataFrame:
    """新浪历史日 K。"""
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
           "CN_MarketDataService.getKLineData")
    params = {"symbol": _sina_prefix(code), "scale": 240, "ma": 5, "datalen": days}
    r = requests.get(url, params=params, timeout=10, headers=_UA)
    r.raise_for_status()
    rows = r.json() or []
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"day": "date", "volume": "vol"})
    for col in ("open", "high", "low", "close", "vol"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_concept_strength(code: str) -> dict:
    """东财概念板块榜单 Top 20，含龙头与涨幅。skill 端可基于此与 fact pack 关联。

    简化版：不做"该票属于哪个概念"反查；返回首个概念占位。
    """
    url = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=200&fid=f3&fs=m:90+t:3&fltt=2&invt=2"
           "&fields=f12,f14,f3,f104")
    r = requests.get(url, timeout=10, headers=_UA)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    diff = data.get("diff") or []
    top = []
    for row in diff[:20]:
        top.append({
            "concept_code": row.get("f12"),
            "concept_name": row.get("f14"),
            "pct_chg": row.get("f3"),
            "leader_name": row.get("f104"),
        })
    return {
        "concept_name": top[0]["concept_name"] if top else None,
        "rank": None,
        "top_concepts": top,
    }


def fetch_money_flow(code: str, days: int = 5) -> pd.DataFrame:
    """东财个股资金流 N 日。"""
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?secid={market}.{code}&klt=101&lmt={days}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65")
    r = requests.get(url, timeout=10, headers=_UA)
    r.raise_for_status()
    klines = ((r.json() or {}).get("data") or {}).get("klines") or []
    parsed = []
    for line in klines:
        cols = line.split(",")
        if len(cols) < 7:
            continue
        parsed.append({
            "date": cols[0],
            "main_in": float(cols[1] or 0),
            "small_in": float(cols[2] or 0),
            "medium_in": float(cols[3] or 0),
            "large_in": float(cols[4] or 0),
            "super_in": float(cols[5] or 0),
        })
    return pd.DataFrame(parsed)


def fetch_recent_news(code: str, days: int = 7) -> list[dict]:
    """同花顺个股新闻搜索；失败/无结果返回 [] (不抛错)。"""
    url = (f"https://news.10jqka.com.cn/tapp/news/push/stock/"
           f"?page=1&tag=&track=website&pagesize=20&code={code}")
    try:
        r = requests.get(url, timeout=8, headers=_UA)
        r.raise_for_status()
        items = ((r.json() or {}).get("data") or {}).get("list") or []
    except Exception:
        return []
    out = []
    for it in items[:10]:
        out.append({
            "title": (it.get("title") or "").strip(),
            "url": it.get("url") or "",
            "date": (it.get("ctime") or it.get("rtime") or "")[:10],
        })
    return out

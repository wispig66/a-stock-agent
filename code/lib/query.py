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

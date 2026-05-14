"""交易日历查询。数据来源：data/trade_calendar.csv（由 refresh_calendar.py 维护）。"""
from __future__ import annotations
import bisect
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALENDAR_FILE = ROOT / "data" / "trade_calendar.csv"


class CalendarOutOfRange(Exception):
    """请求的日期超出本地日历覆盖范围。"""


@lru_cache(maxsize=1)
def _load() -> list[date]:
    if not CALENDAR_FILE.exists():
        raise CalendarOutOfRange(f"交易日历文件不存在：{CALENDAR_FILE}，请先跑 refresh_calendar.py")
    days: list[date] = []
    for i, line in enumerate(CALENDAR_FILE.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or i == 0:  # 跳过 header
            continue
        days.append(datetime.strptime(line, "%Y-%m-%d").date())
    return sorted(days)


def _cache_clear() -> None:
    """测试用：刷新缓存。"""
    _load.cache_clear()


def is_trade_day(d: date) -> bool:
    days = _load()
    idx = bisect.bisect_left(days, d)
    return idx < len(days) and days[idx] == d


def next_trade_day(d: date) -> date:
    """返回严格大于 d 的下一个交易日。"""
    days = _load()
    if d < days[0]:
        raise CalendarOutOfRange(
            f"日历从 {days[0]} 起，无法回答 {d} 之后的下一交易日；请刷新日历"
        )
    idx = bisect.bisect_right(days, d)
    if idx >= len(days):
        raise CalendarOutOfRange(
            f"日历覆盖到 {days[-1]}，无法回答 {d} 之后的下一交易日；请刷新日历"
        )
    return days[idx]


def trade_days_between(a: date, b: date) -> int:
    """[a, b] 闭区间内交易日数 - 1。a==b 且是交易日返回 0。"""
    if a > b:
        a, b = b, a
    days = _load()
    lo = bisect.bisect_left(days, a)
    hi = bisect.bisect_right(days, b)
    n = hi - lo
    return max(n - 1, 0)

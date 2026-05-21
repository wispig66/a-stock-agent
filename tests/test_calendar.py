from datetime import date
from pathlib import Path
import pytest

from stock_codex.domain import calendar as cal


@pytest.fixture
def tiny_csv(tmp_path: Path, monkeypatch) -> Path:
    """造一个最小日历：2026-05-14 (周四) / 05-15 (周五) / 05-18 (周一)，
    跳过 05-16 / 05-17 周末。"""
    p = tmp_path / "trade_calendar.csv"
    p.write_text("trade_date\n2026-05-14\n2026-05-15\n2026-05-18\n", encoding="utf-8")
    monkeypatch.setattr(cal, "CALENDAR_FILE", p)
    cal._cache_clear()
    return p


def test_is_trade_day_true(tiny_csv):
    assert cal.is_trade_day(date(2026, 5, 14)) is True


def test_is_trade_day_false_weekend(tiny_csv):
    assert cal.is_trade_day(date(2026, 5, 16)) is False


def test_next_trade_day_skips_weekend(tiny_csv):
    # 周五 buy → 下一交易日是周一
    assert cal.next_trade_day(date(2026, 5, 15)) == date(2026, 5, 18)


def test_next_trade_day_from_non_trade(tiny_csv):
    # 周六问下一交易日，仍是周一
    assert cal.next_trade_day(date(2026, 5, 16)) == date(2026, 5, 18)


def test_trade_days_between(tiny_csv):
    # 05-14 到 05-18 之间含 14/15/18 三个交易日，间隔 = 2
    assert cal.trade_days_between(date(2026, 5, 14), date(2026, 5, 18)) == 2


def test_next_trade_day_out_of_range_raises(tiny_csv):
    # 日历只到 05-18，问 05-19 之后没数据 → 抛错而不是返回 None
    with pytest.raises(cal.CalendarOutOfRange):
        cal.next_trade_day(date(2026, 5, 18))

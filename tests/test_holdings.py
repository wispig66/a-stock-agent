from datetime import date
from pathlib import Path
import pytest
import yaml

from stock_codex.domain import calendar as cal
from stock_codex.domain import holdings as h


@pytest.fixture
def tmp_yaml(tmp_path: Path, monkeypatch) -> Path:
    """指向 tmp 的 holdings.yaml，并装一个 mini 日历支持 2026-05-14 / 05-15。"""
    cal_csv = tmp_path / "trade_calendar.csv"
    cal_csv.write_text("trade_date\n2026-05-14\n2026-05-15\n2026-05-18\n", encoding="utf-8")
    monkeypatch.setattr(cal, "CALENDAR_FILE", cal_csv)
    cal._cache_clear()

    yml = tmp_path / "holdings.yaml"
    yml.write_text("holdings: []\n", encoding="utf-8")
    monkeypatch.setattr(h, "HOLDINGS_FILE", yml)
    monkeypatch.setattr(h, "LOCK_FILE", tmp_path / "holdings.yaml.lock")
    return yml


def test_read_empty(tmp_yaml):
    assert h.read_holdings() == []


def test_upsert_new(tmp_yaml):
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.02, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.90, take_profit=9.50,
        source="manual",
    )
    h.upsert_holding(rec)
    got = h.read_holdings()
    assert len(got) == 1
    assert got[0].code == "000601"
    assert got[0].unlock_date == date(2026, 5, 15)  # next trade day after 05-14


def test_upsert_merge_weighted_avg(tmp_yaml):
    """同 code 加仓：cost 加权均价，shares 累加，unlock_date 取最新一笔。"""
    base = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    addon = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.5, shares=1000,
        buy_date=date(2026, 5, 15),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(base)
    h.upsert_holding(addon)
    got = h.read_holdings()
    assert len(got) == 1
    merged = got[0]
    assert merged.shares == 2000
    assert merged.cost == pytest.approx(9.25)  # (9.0*1000 + 9.5*1000) / 2000
    assert merged.unlock_date == date(2026, 5, 18)  # next trade day after 05-15


def test_is_locked(tmp_yaml):
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(rec)
    got = h.read_holdings()[0]
    assert got.is_locked(date(2026, 5, 14)) is True
    assert got.is_locked(date(2026, 5, 15)) is False  # unlock_date 当日已解锁
    assert got.is_locked(date(2026, 5, 18)) is False


def test_remove_holding(tmp_yaml):
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(rec)
    removed = h.remove_holding("000601")
    assert removed.code == "000601"
    assert h.read_holdings() == []


def test_reduce_holding_partial_and_full(tmp_yaml):
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(rec)

    old, remaining = h.reduce_holding("000601", 400)
    assert old.shares == 1000
    assert remaining is not None
    assert remaining.shares == 600
    assert h.read_holdings()[0].shares == 600

    old, remaining = h.reduce_holding("000601", 600)
    assert old.shares == 600
    assert remaining is None
    assert h.read_holdings() == []


def test_remove_missing_raises(tmp_yaml):
    with pytest.raises(KeyError):
        h.remove_holding("999999")


def test_legacy_record_without_unlock_date(tmp_yaml):
    """老条目缺 unlock_date / source：视为已解锁、source=manual。"""
    tmp_yaml.write_text(yaml.safe_dump({
        "holdings": [{
            "code": "600000", "name": "浦发银行", "genre": "C",
            "cost": 10.0, "shares": 500,
            "buy_date": "2026-04-01",
            "stop_loss": 9.5, "take_profit": 11.0,
            "note": "历史持仓",
        }]
    }), encoding="utf-8")
    got = h.read_holdings()
    assert len(got) == 1
    assert got[0].unlock_date == date(2026, 4, 1)  # buy_date 兜底，等价已解锁
    assert got[0].is_locked(date(2026, 5, 14)) is False
    assert got[0].source == "manual"


def test_yaml_valid_after_write(tmp_yaml):
    """写入后 yaml 应能被完整解析（验证文件格式正确，非并发原子性测试）。"""
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(rec)
    # 文件应能被 yaml 完整解析（没有半截）
    parsed = yaml.safe_load(tmp_yaml.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict) and "holdings" in parsed

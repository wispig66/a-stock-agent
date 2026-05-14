"""code/lib/query.py 元数据 / 解析单元测试。

注：不联网，is_suspended_today 用本地 DB 当日 daily_kline 是否有记录判断。
"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from lib import query  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    conn.executemany(
        "INSERT INTO stock_basic(code,name,board,list_date,is_st,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("600519", "贵州茅台",   "main",    "2001-08-27", 0, "2026-05-14"),
            ("300750", "宁德时代",   "chinext", "2018-06-11", 0, "2026-05-14"),
            ("688981", "中芯国际",   "star",    "2020-07-16", 0, "2026-05-14"),
            ("835174", "五新隧装",   "bse",     "2021-11-15", 0, "2026-05-14"),
            ("000725", "ST 京东方",  "main",    "1997-06-19", 1, "2026-05-14"),
            ("600000", "浦发银行",   "main",    "1999-11-10", 0, "2026-05-14"),
        ],
    )
    conn.commit()
    monkeypatch.setattr(query, "DB", p)
    return p


def test_parse_input_pure_code():
    assert query.parse_input("600519") == ("code", "600519")
    assert query.parse_input(" 600519 ") == ("code", "600519")


def test_parse_input_strips_prefix():
    assert query.parse_input("SH600519") == ("code", "600519")
    assert query.parse_input("sz300750") == ("code", "300750")
    assert query.parse_input("$600519") == ("code", "600519")
    assert query.parse_input("#600519") == ("code", "600519")


def test_parse_input_chinese_name():
    assert query.parse_input("贵州茅台") == ("name", "贵州茅台")
    assert query.parse_input("茅台") == ("name", "茅台")


def test_parse_input_rejects_garbage():
    assert query.parse_input("你好") == ("name", "你好")  # 含中文走 name 路径，由 lookup_by_name 兜底
    assert query.parse_input("12345") == ("unknown", "12345")
    assert query.parse_input("") == ("unknown", "")


def test_board_of(db):
    assert query.board_of("600519") == "main"
    assert query.board_of("300750") == "chinext"
    assert query.board_of("688981") == "star"
    assert query.board_of("835174") == "bse"
    assert query.board_of("999999") is None


def test_is_st(db):
    assert query.is_st("000725") is True
    assert query.is_st("600519") is False
    assert query.is_st("999999") is False


def test_lookup_by_name_exact(db):
    hits = query.lookup_by_name("贵州茅台")
    assert hits == [("600519", "贵州茅台")]


def test_lookup_by_name_substring(db):
    hits = query.lookup_by_name("茅台")
    assert ("600519", "贵州茅台") in hits
    assert len(hits) == 1


def test_lookup_by_name_miss(db):
    assert query.lookup_by_name("不存在公司") == []


def test_is_suspended_today_no_kline(db):
    # daily_kline 当日无记录 → 视为停牌
    assert query.is_suspended_today("600519", today="2026-05-14") is True


def test_is_suspended_today_has_kline(db):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO daily_kline(code,date,open,high,low,close,vol,amount,pct_chg) "
        "VALUES ('600519','2026-05-14',1600,1610,1590,1605,1e6,1.6e9,0.5)"
    )
    conn.commit()
    conn.close()
    assert query.is_suspended_today("600519", today="2026-05-14") is False

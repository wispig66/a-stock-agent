"""sector_pack 四个 _fetch_*_panel 实现验证。
HTTP 用 responses 桩，DB 用 fixture 注入。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from lib import sector_pack  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    # 当日 + 近 5 日涨停题材
    for d in ["2026-05-10", "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14"]:
        conn.execute("INSERT INTO limit_up(date,code,concept,name) VALUES(?,?,?,?)",
                     (d, "300750", "光伏", "宁德"))
        conn.execute("INSERT INTO limit_up(date,code,concept,name) VALUES(?,?,?,?)",
                     (d, "600438", "光伏", "通威"))
    conn.execute("INSERT INTO ths_hot_reason(date,code,reason) VALUES('2026-05-14','300750','光伏 + 储能')")
    conn.execute("INSERT INTO ths_hot_reason(date,code,reason) VALUES('2026-05-13','600438','光伏龙头')")
    conn.commit()
    monkeypatch.setattr(sector_pack, "DB", p)
    return p


def test_news_panel_reads_ths_hot_reason(db):
    panel = sector_pack._fetch_news_panel("光伏")
    titles = [n["title"] for n in panel["top_news"]]
    assert any("光伏" in t for t in titles)


def test_fundamental_panel_counts_members(db):
    panel = sector_pack._fetch_fundamental_panel("光伏")
    assert panel["member_count"] >= 2   # 300750 + 600438
    assert panel["industry"]            # 即便是 "未分类" 也要有值


def test_sentiment_panel_counts_limit_up(db):
    panel = sector_pack._fetch_sentiment_panel("光伏")
    # 近 5 日命中 "光伏" 题材的涨停股次数
    assert panel["limit_up_count"] >= 5  # 2 只 × 5 天 = 10 次（按日聚合实现可能 = 5，最少 ≥5）
    assert "candidates" in panel

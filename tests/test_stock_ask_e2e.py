"""stock-ask 端到端：4 种意图各跑数据层一次，断言关键字段存在。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

from stock_codex.apps import stock_ask_pipeline  # noqa: E402
from stock_codex.domain import holdings  # noqa: E402
from stock_codex.market import intent, sector_pack, event_pack, query  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.execute("INSERT INTO ths_hot_reason(date,code,reason) VALUES('2026-05-14','300750','光伏 + 储能')")
    conn.execute("INSERT INTO limit_up(date,code,concept,name) VALUES('2026-05-14','300750','光伏','宁德')")
    conn.execute("INSERT INTO stock_basic(code,name,board,list_date,is_st,updated_at) "
                 "VALUES('300750','宁德','chinext','2018-06-11',0,'2026-05-14')")
    conn.commit()
    monkeypatch.setattr(sector_pack, "DB", p)
    return p


def test_e2e_sector_intent(db):
    r = intent.classify("光伏怎么样", lexicon=sector_pack._load_lexicon())
    assert r["intent"] == "sector"
    pack = sector_pack.build_sector_pack(r["extracted"])
    assert pack["sector"] == "光伏"
    assert "panels" in pack
    assert pack["stage"]  # 至少有值


def test_e2e_stock_intent_short_circuits(db):
    r = intent.classify("600519", lexicon=sector_pack._load_lexicon())
    assert r["intent"] == "stock"
    assert r["extracted"] == "600519"
    # 真正的 stock-query 调用走 skill 层，e2e 不验证


def test_stock_match_marks_existing_holding(db, tmp_path, monkeypatch):
    holdings_file = tmp_path / "holdings.yaml"
    holdings_file.write_text(
        "holdings:\n"
        "- code: '300750'\n"
        "  name: 宁德\n"
        "  genre: B\n"
        "  cost: 200.0\n"
        "  shares: 100\n"
        "  buy_date: '2026-05-14'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(query, "DB", db)
    monkeypatch.setattr(holdings, "HOLDINGS_FILE", holdings_file)
    monkeypatch.setattr(holdings, "LOCK_FILE", tmp_path / "holdings.yaml.lock")

    matched = stock_ask_pipeline.task_stock_match("300750")

    assert matched["matched"] is True
    assert matched["is_holding"] is True


def test_e2e_event_intent(db):
    r = intent.classify("国常会批了储能补贴", lexicon=sector_pack._load_lexicon())
    assert r["intent"] == "event"
    fake_categorize = lambda t: {
        "event_type": "政策",
        "candidate_sectors": ["储能", "光伏"],
        "risk_sectors": [],
        "core_logic": "补贴落地"
    }
    pack = event_pack.build_event_pack(
        r["extracted"], mode="normal",
        categorize=fake_categorize,
        sector_pack_fn=sector_pack.build_sector_pack,
    )
    assert pack["benefit_sectors"]
    assert pack["event_type"] == "政策"


def test_e2e_ambiguous_intent(db):
    r = intent.classify("xxx 不知道是啥", lexicon=set())
    assert r["intent"] == "ambiguous"
    assert "candidates" in r["extracted"]
    # With empty lexicon, _nearest_sector returns None → no sector candidate A.
    # Only stock (B) and event (C) candidates are produced → 2 candidates.
    assert len(r["extracted"]["candidates"]) >= 2

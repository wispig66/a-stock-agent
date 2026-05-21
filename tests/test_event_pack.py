"""event_pack：归类 + 题材库校准 + normal/deep + 全 ✗ 降档。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

from stock_codex.market import event_pack, sector_pack  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.execute("INSERT INTO ths_hot_reason(date,code,reason) VALUES('2026-05-14','300750','储能 + 新能源')")
    conn.execute("INSERT INTO limit_up(date,code,concept) VALUES('2026-05-14','300750','储能')")
    conn.commit()
    monkeypatch.setattr(sector_pack, "DB", p)
    return p


def fake_categorize(text):
    return {
        "event_type": "政策",
        "candidate_sectors": ["储能", "新能源", "电网设备"],
        "risk_sectors": ["传统火电"],
        "core_logic": "补贴落地→直接利好"
    }


def test_calibrate_three_buckets(db):
    fake_pack = lambda s: {"sector": s, "stage": "启动期", "top_n": [], "panels": {}, "verdict_modifiers": []}
    pack = event_pack.build_event_pack(
        "国常会批了储能补贴",
        mode="normal",
        categorize=fake_categorize,
        sector_pack_fn=fake_pack,
    )
    # 储能、新能源 应 ✓，电网设备 △ 或 ✗
    labels = {s["name"]: s["calibration"] for s in pack["benefit_sectors"]}
    assert labels.get("储能") == "verified"


def test_all_miss_degrades(db):
    # 所有 candidate_sectors 都不在 lexicon → 保留前 2 个 + degraded=True
    def miss_categorize(text):
        return {
            "event_type": "突发",
            "candidate_sectors": ["未知方向A", "未知方向B", "未知方向C"],
            "risk_sectors": [],
            "core_logic": "..."
        }
    fake_pack = lambda s: {"sector": s, "stage": "未知", "top_n": [], "panels": {}, "verdict_modifiers": []}
    pack = event_pack.build_event_pack(
        "全新事件", mode="normal", categorize=miss_categorize, sector_pack_fn=fake_pack,
    )
    assert pack["degraded"] is True
    assert len(pack["benefit_sectors"]) == 2  # 保留前 2 个


def test_deep_mode_web_timeout_silent_degrade(db, monkeypatch):
    def web_too_slow(query, timeout):
        raise TimeoutError("> 45s")
    fake_pack = lambda s: {"sector": s, "stage": "启动期", "top_n": [], "panels": {}, "verdict_modifiers": []}
    pack = event_pack.build_event_pack(
        "储能补贴",
        mode="deep",
        categorize=fake_categorize,
        sector_pack_fn=fake_pack,
        web_fetch=web_too_slow,
    )
    assert pack["mode"] == "deep"
    assert pack["web_status"] == "timeout"
    # 仍能给受益板块，未因 web 超时崩
    assert pack["benefit_sectors"]


def test_risk_sectors_listed_not_recommended(db):
    fake_pack = lambda s: {"sector": s, "stage": "退潮期", "top_n": [{"code": "X"}], "panels": {}, "verdict_modifiers": []}
    pack = event_pack.build_event_pack(
        "国常会批了储能补贴",
        mode="normal", categorize=fake_categorize, sector_pack_fn=fake_pack,
    )
    # 风险板块只列名，不进 recommendations
    rec_sectors = {r["sector"] for r in pack["recommendations"]}
    assert "传统火电" not in rec_sectors
    risk_names = {s["name"] for s in pack["risk_sectors"]}
    assert "传统火电" in risk_names

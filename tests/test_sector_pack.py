"""sector_pack：模糊匹配 + 四面板并发 + 阶段判定 + Top 5 选股。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

from stock_codex.market import sector_pack  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    # 1 条今日同花顺热点 + 1 条历史涨停题材，构成 lexicon
    conn.execute("INSERT INTO ths_hot_reason(date,code,reason) VALUES('2026-05-14','600519','光伏概念')")
    conn.execute("INSERT INTO limit_up(date,code,concept) VALUES('2026-05-14','600519','光伏')")
    conn.commit()
    monkeypatch.setattr(sector_pack, "DB", p)
    return p


def test_fuzzy_match_exact(db):
    assert sector_pack.fuzzy_match("光伏") == "光伏"

def test_fuzzy_match_synonym(db):
    # 内置同义词："AI" → "人工智能"（同义词表中未命中 DB 则保留原词）
    # 实现：先精确，再子串，最后内置同义词表
    monkey = {"AI": "人工智能"}
    assert sector_pack.fuzzy_match("AI", synonyms=monkey, allow_external=False) == "AI"  # DB 没有则原样返回

def test_fuzzy_match_not_found(db):
    with pytest.raises(sector_pack.SectorNotFound) as ex:
        sector_pack.fuzzy_match("光福")
    # 异常应携带最相近的 3 个候选
    assert ex.value.candidates  # 非空

def test_top5_filter_st_and_topspike(db):
    candidates = [
        {"code": "000001", "name": "平安银行", "is_st": False, "ret_5d": 12, "main_inflow_3d": 5, "dist_high_20d_pct": 8, "limit_up_lock": False},
        {"code": "000725", "name": "ST京东方", "is_st": True,  "ret_5d": 20, "main_inflow_3d": 3, "dist_high_20d_pct": 5, "limit_up_lock": False},
        {"code": "300750", "name": "宁德",     "is_st": False, "ret_5d": 25, "main_inflow_3d": 2, "dist_high_20d_pct": 1, "limit_up_lock": False},  # 追高
        {"code": "600519", "name": "茅台",     "is_st": False, "ret_5d": 5,  "main_inflow_3d": 1, "dist_high_20d_pct": 10, "limit_up_lock": True},   # 封死
        {"code": "600000", "name": "浦发",     "is_st": False, "ret_5d": 8,  "main_inflow_3d": 4, "dist_high_20d_pct": 6, "limit_up_lock": False},
        {"code": "688981", "name": "中芯",     "is_st": False, "ret_5d": 6,  "main_inflow_3d": 2, "dist_high_20d_pct": 7, "limit_up_lock": False},
    ]
    top = sector_pack.pick_top_n(candidates, n=5)
    codes = [c["code"] for c in top]
    assert "000725" not in codes  # ST 剔除
    assert "300750" not in codes  # 距高 < 3% 剔除
    assert "600519" not in codes  # 涨停封死剔除
    assert "000001" in codes


def test_stage_classification():
    # 启动期：5 日 5-15% + 龙头刚加速
    assert sector_pack.classify_stage(ret_5d_pct=10, limit_up_count=2, leader_consecutive=1, ret_3d_pct=8) == "启动期"
    # 主升期：>15% + 涨停股 >=3
    assert sector_pack.classify_stage(ret_5d_pct=18, limit_up_count=4, leader_consecutive=2, ret_3d_pct=12) == "主升期"
    # 高潮期：龙头连板 >=4
    assert sector_pack.classify_stage(ret_5d_pct=25, limit_up_count=5, leader_consecutive=4, ret_3d_pct=15) == "高潮期"
    # 退潮期：近 3 日累跌
    assert sector_pack.classify_stage(ret_5d_pct=8, limit_up_count=1, leader_consecutive=0, ret_3d_pct=-3) == "退潮期"


def test_panel_failure_degrades(monkeypatch, db):
    """情绪面拉取异常 → 该面板标 error，结论 verdict 降一档。"""
    def boom(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(sector_pack, "_fetch_sentiment_panel", boom)
    monkeypatch.setattr(sector_pack, "_fetch_news_panel",       lambda s: {"top_news": []})
    monkeypatch.setattr(sector_pack, "_fetch_fundamental_panel",lambda s: {"member_count": 50})
    monkeypatch.setattr(sector_pack, "_fetch_technical_panel",  lambda s: {"ma_position": "之上"})
    pack = sector_pack.build_sector_pack("光伏")
    assert pack["panels"]["sentiment"]["ok"] is False
    assert "数据缺失" in pack["verdict_modifiers"][0]

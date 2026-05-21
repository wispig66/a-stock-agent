"""weekly_pack.build_weekly_data_pack 单测。

构造一周本地数据 → 验证 5 个交易日聚合 + 板块周涨幅 + 个人交易归类。
"""
from __future__ import annotations
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Seed a temp DB with one full week (2026-05-11 ~ 2026-05-15) of fake data."""
    db_path = tmp_path / "daily.db"
    conn = sqlite3.connect(db_path)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())

    # 5 个交易日 × 2 只股票（300308 算力 / 600519 白酒）
    days = ["2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15"]
    for i, d in enumerate(days):
        conn.execute(
            "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,?)",
            ("300308", d, 100+i, 105+i, 99+i, 104+i, 1e7, 1e9, 2.0),
        )
        conn.execute(
            "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,?)",
            ("600519", d, 1700, 1705, 1695, 1700-i, 1e6, 1e9, -0.5),
        )
        conn.execute(
            "INSERT INTO sentiment_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d, 80-i*5, 5+i, 5-i, 0.5, 0.4, 0.3, 0.2, i, "退潮期"),
        )
    # 1 笔个人交易（5/13 买入 300308）
    conn.execute(
        "INSERT INTO trades(ts, code, side, price, qty, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-13T09:35:00", "300308", "buy", 102.5, 1000, "L1_breakout"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("STOCK_DB_PATH", str(db_path))
    return db_path


def test_build_weekly_data_pack_basic(seeded_db):
    from stock_codex.market.weekly_pack import build_weekly_data_pack

    pack = build_weekly_data_pack(end_date=date(2026, 5, 17))  # Sunday

    assert pack["week_label"] == "2026-W20"
    assert pack["monday"] == "2026-05-11"
    assert pack["friday"] == "2026-05-15"
    assert pack["trading_days_in_week"] == 5
    assert len(pack["sentiment_series"]) == 5
    assert pack["sentiment_series"][0]["date"] == "2026-05-11"
    # 300308 全周从 100 涨到 108（5/11 收 104 → 5/15 收 108）
    top_gainers = pack["top_gainers"]
    assert any(s["code"] == "300308" for s in top_gainers)
    # trades 归到 weekly_trades
    assert len(pack["weekly_trades"]) == 1
    assert pack["weekly_trades"][0]["code"] == "300308"


def test_build_weekly_data_pack_empty_week(tmp_path, monkeypatch):
    """空仓周 / 无数据：返回结构完整，weekly_trades 为空。"""
    db_path = tmp_path / "daily.db"
    conn = sqlite3.connect(db_path)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.close()
    monkeypatch.setenv("STOCK_DB_PATH", str(db_path))

    from stock_codex.market.weekly_pack import build_weekly_data_pack
    pack = build_weekly_data_pack(end_date=date(2026, 5, 17))

    assert pack["weekly_trades"] == []
    assert pack["top_gainers"] == []
    assert pack["trading_days_in_week"] == 0  # 无 daily_kline → 0


def test_render_long_form_sections(seeded_db):
    from stock_codex.market.weekly_pack import build_weekly_data_pack, render_long_form

    pack = build_weekly_data_pack(end_date=date(2026, 5, 17))
    parts = {
        "part1_narrative": "本周情绪退潮，算力主线高位震荡。",
        "part2_narrative": "下周关注算力延续 + 创新药。",
        "themes": [
            {
                "name": "算力硬件",
                "stance": "延续",
                "leaders": ["300308"],
                "catalysts": [{"date": "2026-05-22", "event": "英伟达财报"}],
                "risks": ["美债收益率上行"],
                "match_score": "high",
            }
        ],
        "discipline_notes": "龙头股加速期不追，缓涨期可埋伏。",
        "web_status": "ok",
    }
    md = render_long_form(pack, parts)

    assert "# W20 周复盘" in md
    assert "## Part 1 本周复盘" in md
    assert "## Part 2 下周方向" in md
    assert "## 下周方向 (machine-readable)" in md
    assert "themes:" in md
    assert "算力硬件" in md
    assert "match_score: high" in md


def test_parse_machine_readable_roundtrip(seeded_db, tmp_path):
    from stock_codex.market.weekly_pack import (
        build_weekly_data_pack, render_long_form, parse_machine_readable,
    )
    pack = build_weekly_data_pack(end_date=date(2026, 5, 17))
    parts = {
        "part1_narrative": "x",
        "part2_narrative": "y",
        "themes": [{"name": "算力", "stance": "延续",
                    "leaders": ["300308"], "catalysts": [],
                    "risks": [], "match_score": "high"}],
        "discipline_notes": "z",
        "web_status": "ok",
    }
    md = render_long_form(pack, parts)
    out = tmp_path / "w20.md"
    out.write_text(md)

    parsed = parse_machine_readable(out)
    assert parsed is not None
    assert parsed["week"] == "2026-W20"
    assert parsed["themes"][0]["name"] == "算力"
    assert parsed["web_status"] == "ok"


def test_parse_machine_readable_missing(tmp_path):
    from stock_codex.market.weekly_pack import parse_machine_readable
    p = tmp_path / "nope.md"
    p.write_text("# 普通 markdown，没有 yaml 块\n")
    assert parse_machine_readable(p) is None

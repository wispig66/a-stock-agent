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
sys.path.insert(0, str(ROOT / "code"))


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Seed a temp DB with one full week (2026-05-11 ~ 2026-05-15) of fake data."""
    db_path = tmp_path / "daily.db"
    conn = sqlite3.connect(db_path)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())

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
    from lib.weekly_pack import build_weekly_data_pack

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
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    conn.close()
    monkeypatch.setenv("STOCK_DB_PATH", str(db_path))

    from lib.weekly_pack import build_weekly_data_pack
    pack = build_weekly_data_pack(end_date=date(2026, 5, 17))

    assert pack["weekly_trades"] == []
    assert pack["top_gainers"] == []
    assert pack["trading_days_in_week"] == 0  # 无 daily_kline → 0

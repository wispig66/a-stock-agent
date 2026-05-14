"""refresh_stock_basic 烟雾测试：mock 接口返回 3 行，断言写入 stock_basic。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_stock_basic as r  # noqa: E402


def test_upsert_rows(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    conn.close()
    monkeypatch.setattr(r, "DB", db)

    fake_rows = [
        {"code": "600519", "name": "贵州茅台", "board": "main",    "list_date": "2001-08-27", "is_st": 0},
        {"code": "300750", "name": "宁德时代", "board": "chinext", "list_date": "2018-06-11", "is_st": 0},
        {"code": "000725", "name": "*ST 京东方","board": "main",   "list_date": "1997-06-19", "is_st": 1},
    ]
    with patch.object(r, "fetch_all_stock_basic", return_value=fake_rows):
        r.main()

    conn = sqlite3.connect(db)
    rows = sorted(conn.execute(
        "SELECT code,name,board,is_st FROM stock_basic ORDER BY code").fetchall())
    assert rows == [
        ("000725", "*ST 京东方", "main",    1),
        ("300750", "宁德时代",   "chinext", 0),
        ("600519", "贵州茅台",   "main",    0),
    ]


def test_board_inference():
    assert r.infer_board("600519") == "main"
    assert r.infer_board("000725") == "main"
    assert r.infer_board("002001") == "main"
    assert r.infer_board("300750") == "chinext"
    assert r.infer_board("688981") == "star"
    assert r.infer_board("835174") == "bse"
    assert r.infer_board("430139") == "bse"

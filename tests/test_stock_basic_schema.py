"""验证 stock_basic 表 schema 与初始化脚本可重入执行。"""
from __future__ import annotations
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = ROOT / "code" / "init_db.sql"


def test_stock_basic_schema(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(SQL_FILE.read_text())
    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(stock_basic)")}
    assert cols == {
        "code": "TEXT",
        "name": "TEXT",
        "board": "TEXT",
        "list_date": "TEXT",
        "is_st": "INTEGER",
        "updated_at": "TEXT",
    }
    pks = [r[1] for r in conn.execute("PRAGMA table_info(stock_basic)") if r[5]]
    assert pks == ["code"]
    # 可重入
    conn.executescript(SQL_FILE.read_text())
    conn.close()

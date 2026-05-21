"""tg_inbound 表 schema + 索引存在性测试。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_tg_inbound_table_and_indexes(tmp_path):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())

    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(tg_inbound)")}
    assert cols["timestamp"] == "TEXT"
    assert cols["update_id"] == "INTEGER"
    assert cols["chat_id"] == "TEXT"
    assert cols["user_msg_id"] == "INTEGER"
    assert cols["raw_text"] == "TEXT"
    assert cols["parsed_command"] == "TEXT"
    assert cols["parsed_intent"] == "TEXT"
    assert cols["parsed_payload"] == "TEXT"
    assert cols["response_msg_id"] == "INTEGER"
    assert cols["handler_status"] == "TEXT"
    assert cols["handler_error"] == "TEXT"
    assert cols["duration_ms"] == "INTEGER"

    idx = {r[1] for r in conn.execute("PRAGMA index_list(tg_inbound)")}
    assert "idx_tg_inbound_ts" in idx
    assert "idx_tg_inbound_chat" in idx
    assert "idx_tg_inbound_command" in idx

    # update_id UNIQUE constraint
    conn.execute("INSERT INTO tg_inbound(timestamp,update_id,chat_id,user_msg_id,raw_text) "
                 "VALUES('2026-05-15T10:00:00', 1, 'c', 1, 'x')")
    try:
        conn.execute("INSERT INTO tg_inbound(timestamp,update_id,chat_id,user_msg_id,raw_text) "
                     "VALUES('2026-05-15T10:00:01', 1, 'c', 2, 'y')")
        pytest.fail("应触发 UNIQUE 冲突")
    except sqlite3.IntegrityError:
        pass

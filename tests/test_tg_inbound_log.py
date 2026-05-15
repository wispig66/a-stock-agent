"""tg_inbound 落库 + /ask /ask+ 解析单测。"""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "code"))

import tg_listener as tl  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    conn.commit()
    monkeypatch.setattr(tl, "DB_PATH", p)
    return p


def test_parse_ask_normal():
    out = tl.parse_ask_command("/ask 光伏怎么样")
    assert out == {"mode": "normal", "payload": "光伏怎么样"}

def test_parse_ask_deep():
    out = tl.parse_ask_command("/ask+ 国常会批了储能补贴")
    assert out == {"mode": "deep", "payload": "国常会批了储能补贴"}

def test_parse_ask_empty():
    assert tl.parse_ask_command("/ask") is None
    assert tl.parse_ask_command("/ask+   ") is None

def test_parse_ask_explicit_override():
    out = tl.parse_ask_command("/ask sector=光伏")
    assert out == {"mode": "normal", "payload": "sector=光伏"}


def test_timeout_per_mode():
    assert tl.skill_timeout_for("normal") == 180
    assert tl.skill_timeout_for("deep") == 300


def test_log_inbound_round_trip(db):
    inbound_id = tl.log_inbound_start(
        update_id=42, chat_id="c1", user_msg_id=100, raw_text="/ask 光伏"
    )
    assert inbound_id

    tl.log_inbound_update_parsed(inbound_id, parsed_command="/ask",
                                  parsed_intent="sector", parsed_payload={"sector": "光伏"})
    tl.log_inbound_finish(inbound_id, response_msg_id=200, status="ok", duration_ms=12345)

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT update_id,parsed_command,parsed_intent,parsed_payload,"
                           "response_msg_id,handler_status,duration_ms FROM tg_inbound "
                           "WHERE id=?", (inbound_id,)).fetchone()
    assert row[0] == 42
    assert row[1] == "/ask"
    assert row[2] == "sector"
    assert json.loads(row[3])["sector"] == "光伏"
    assert row[4] == 200
    assert row[5] == "ok"
    assert row[6] == 12345


def test_log_inbound_duplicate_update_id(db):
    first = tl.log_inbound_start(update_id=99, chat_id="c", user_msg_id=1, raw_text="x")
    dup   = tl.log_inbound_start(update_id=99, chat_id="c", user_msg_id=2, raw_text="y")
    assert first is not None
    assert dup is None


def test_log_inbound_timeout_path(db):
    inbound_id = tl.log_inbound_start(update_id=1, chat_id="c", user_msg_id=1, raw_text="/ask+ x")
    tl.log_inbound_finish(inbound_id, response_msg_id=None,
                          status="timeout", duration_ms=180000,
                          error="子进程超时")
    with sqlite3.connect(db) as conn:
        s, e = conn.execute("SELECT handler_status, handler_error FROM tg_inbound WHERE id=?",
                            (inbound_id,)).fetchone()
    assert s == "timeout"
    assert "超时" in e

"""tg_listener.handle() 单元测试：mock 数据库 + mock subprocess + mock push。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "code"))

import tg_listener as tl  # noqa: E402


def _seed_db(path):
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    conn.executemany(
        "INSERT INTO stock_basic(code,name,board,list_date,is_st,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        [
            ("600519", "贵州茅台",   "main",    "2001", 0, "2026-05-14"),
            ("300750", "宁德时代",   "chinext", "2018", 0, "2026-05-14"),
            ("688981", "中芯国际",   "star",    "2020", 0, "2026-05-14"),
            ("000725", "*ST 京东方", "main",    "1997", 1, "2026-05-14"),
        ],
    )
    conn.execute("INSERT INTO daily_kline(code,date,close) VALUES('600519','2026-05-14',1605)")
    conn.execute("INSERT INTO daily_kline(code,date,close) VALUES('300750','2026-05-14',180)")
    conn.execute("INSERT INTO daily_kline(code,date,close) VALUES('688981','2026-05-14',50)")
    conn.execute("INSERT INTO daily_kline(code,date,close) VALUES('000725','2026-05-14',3)")
    conn.commit()
    conn.close()


def _setup(monkeypatch, tmp_path):
    _seed_db(tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    monkeypatch.setattr(tl, "HOLDINGS_FILE", tmp_path / "holdings.yaml")
    (tmp_path / "holdings.yaml").write_text("holdings: []\n")
    monkeypatch.setattr(tl, "ALLOWED_CHAT_ID", "999")


def test_handle_unknown_silent(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill_streaming") as r:
        tl.handle("12345", chat_id="999", today="2026-05-14")
    p.assert_not_called()
    r.assert_not_called()


def test_handle_star_rejected(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill_streaming") as r:
        tl.handle("688981", chat_id="999", today="2026-05-14")
    p.assert_called_once()
    msg = p.call_args.args[0]
    assert "科创板" in msg
    r.assert_not_called()


def test_handle_st_rejected(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill_streaming") as r:
        tl.handle("000725", chat_id="999", today="2026-05-14")
    p.assert_called_once()
    assert "ST" in p.call_args.args[0]
    r.assert_not_called()


def test_handle_unknown_code(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill_streaming") as r:
        tl.handle("999999", chat_id="999", today="2026-05-14")
    p.assert_called_once()
    assert "未找到" in p.call_args.args[0]
    r.assert_not_called()


def test_handle_chinese_multi_hit(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO stock_basic(code,name,board,is_st,updated_at) "
                 "VALUES('600702','舍得茅台酒','main',0,'2026-05-14')")
    conn.commit(); conn.close()
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill_streaming") as r:
        tl.handle("茅台", chat_id="999", today="2026-05-14")
    p.assert_called_once()
    assert "找到多只" in p.call_args.args[0]
    r.assert_not_called()


def test_handle_fresh_dispatches_skill(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "_tg_send", return_value=42) as send, \
         patch.object(tl, "_tg_edit") as edit, \
         patch.object(tl, "run_skill_streaming",
                      return_value="📊 fake card") as r:
        tl.handle("600519", chat_id="999", today="2026-05-14")
    r.assert_called_once()
    code, mode = r.call_args.args[0], r.call_args.args[1]
    assert code == "600519"
    assert mode == "fresh"
    # 占位发送一次
    send.assert_called_once()
    assert "分析中" in send.call_args.args[0]
    # 最终编辑落到同一条消息（msg_id=42）
    final_call = edit.call_args_list[-1]
    assert final_call.args[0] == 42
    assert "📊" in final_call.args[1] or "fake card" in final_call.args[1]


def test_handle_holding_branch(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    (tmp_path / "holdings.yaml").write_text(
        "holdings:\n  - code: '600519'\n    name: 贵州茅台\n    genre: B\n"
        "    cost: 1580\n    shares: 100\n    buy_date: 2026-05-09\n"
    )
    with patch.object(tl, "_tg_send", return_value=1), \
         patch.object(tl, "_tg_edit"), \
         patch.object(tl, "run_skill_streaming", return_value="📊 fake") as r:
        tl.handle("600519", chat_id="999", today="2026-05-14")
    assert r.call_args.args[1] == "holding"


def test_handle_wrong_chat_id_silent(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(tl, "ALLOWED_CHAT_ID", "12345")
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "_tg_send") as send, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("600519", chat_id="999", today="2026-05-14")
    p.assert_not_called()
    send.assert_not_called()
    r.assert_not_called()


def test_handle_chinese_no_hit(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("根本不存在公司名", chat_id="999", today="2026-05-14")
    p.assert_called_once()
    assert "未找到" in p.call_args.args[0]
    r.assert_not_called()

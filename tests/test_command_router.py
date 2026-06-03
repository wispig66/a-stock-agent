"""command_router.handle() 单元测试：mock 数据库 + mock subprocess + mock 发送。

（前身 test_tg_listener.py；移除 Telegram 传输/轮询专属用例后迁来。）
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from unittest.mock import patch
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

from stock_codex.apps import command_router as tl  # noqa: E402
from stock_codex.channels import ChannelMessage, Delivery  # noqa: E402


def _seed_db(path):
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
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
    cal_csv = tmp_path / "trade_calendar.csv"
    cal_csv.write_text("trade_date\n2026-05-14\n2026-05-15\n2026-05-18\n", encoding="utf-8")
    monkeypatch.setattr(tl.holdings_lib.cal, "CALENDAR_FILE", cal_csv)
    tl.holdings_lib.cal._cache_clear()
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    monkeypatch.setattr(tl, "HOLDINGS_FILE", tmp_path / "holdings.yaml")
    monkeypatch.setattr(tl.holdings_lib, "HOLDINGS_FILE", tmp_path / "holdings.yaml")
    monkeypatch.setattr(tl.holdings_lib, "LOCK_FILE", tmp_path / "holdings.yaml.lock")
    (tmp_path / "holdings.yaml").write_text("holdings: []\n")
    # 默认通道 feishu：用 FEISHU 白名单放行 chat_id=999
    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "999")


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
    with patch.object(tl, "send_message", return_value=42) as send, \
         patch.object(tl, "edit_message") as edit, \
         patch.object(tl, "run_skill_streaming",
                      return_value="📊 fake card") as r:
        tl.handle("600519", chat_id="999", today="2026-05-14")
    r.assert_called_once()
    code, mode = r.call_args.args[0], r.call_args.args[1]
    assert code == "600519"
    assert mode == "fresh"
    send.assert_called_once()
    assert "分析中" in send.call_args.args[0]
    final_call = edit.call_args_list[-1]
    assert final_call.args[0] == 42
    assert "📊" in final_call.args[1] or "fake card" in final_call.args[1]


def test_handle_holding_branch(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    (tmp_path / "holdings.yaml").write_text(
        "holdings:\n  - code: '600519'\n    name: 贵州茅台\n    genre: B\n"
        "    cost: 1580\n    shares: 100\n    buy_date: 2026-05-09\n"
    )
    with patch.object(tl, "send_message", return_value=1), \
         patch.object(tl, "edit_message"), \
         patch.object(tl, "run_skill_streaming", return_value="📊 fake") as r:
        tl.handle("600519", chat_id="999", today="2026-05-14")
    assert r.call_args.args[1] == "holding"


def test_parse_watch_command_defaults_to_auto_plan():
    assert tl.parse_watch_command("/watch 002908") == {
        "code": "002908",
        "entry_price": None,
        "stop_price": None,
    }


def test_build_watch_plan_from_realtime_and_kline(monkeypatch):
    monkeypatch.setattr(tl.query, "fetch_realtime", lambda code: {
        "name": "德生科技",
        "close": 7.58,
    })
    monkeypatch.setattr(tl.query, "fetch_kline", lambda code, days=30: pd.DataFrame([
        {"close": 7.10, "high": 7.20, "low": 7.00, "vol": 100},
        {"close": 7.20, "high": 7.30, "low": 7.10, "vol": 110},
        {"close": 7.25, "high": 7.35, "low": 7.15, "vol": 120},
        {"close": 7.30, "high": 7.40, "low": 7.20, "vol": 130},
        {"close": 7.35, "high": 7.45, "low": 7.25, "vol": 140},
        {"close": 7.40, "high": 7.50, "low": 7.30, "vol": 150},
        {"close": 7.45, "high": 7.55, "low": 7.35, "vol": 160},
        {"close": 7.50, "high": 7.60, "low": 7.40, "vol": 170},
        {"close": 7.55, "high": 7.65, "low": 7.45, "vol": 180},
        {"close": 7.58, "high": 7.68, "low": 7.42, "vol": 190},
    ]))

    plan = tl.build_watch_plan("002908")

    assert plan["entry_price"] > 7.58
    assert plan["stop_price"] < plan["entry_price"]
    assert plan["max_chase_price"] > plan["entry_price"]
    assert plan["take_profit_price"] > plan["entry_price"]


def test_handle_watch_analyzes_and_persists_dynamic_watch(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO stock_basic(code,name,board,is_st,updated_at) VALUES('002908','德生科技','main',0,'2026-05-14')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(tl, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(tl, "DB_FILE", tmp_path / "t.db")
    monkeypatch.setattr(tl, "TRADES_DB", tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    monkeypatch.setattr(tl, "DATA_DIR", tmp_path)
    (tmp_path / "trade_calendar.csv").write_text("trade_date\n2026-05-14\n2026-05-15\n", encoding="utf-8")
    monkeypatch.setattr(tl.query, "fetch_realtime", lambda code: {
        "name": "德生科技",
        "close": 7.58,
    })
    monkeypatch.setattr(tl.query, "fetch_kline", lambda code, days=30: pd.DataFrame([
        {"close": 7.10, "high": 7.20, "low": 7.00, "vol": 100},
        {"close": 7.20, "high": 7.30, "low": 7.10, "vol": 110},
        {"close": 7.25, "high": 7.35, "low": 7.15, "vol": 120},
        {"close": 7.30, "high": 7.40, "low": 7.20, "vol": 130},
        {"close": 7.35, "high": 7.45, "low": 7.25, "vol": 140},
        {"close": 7.40, "high": 7.50, "low": 7.30, "vol": 150},
        {"close": 7.45, "high": 7.55, "low": 7.35, "vol": 160},
        {"close": 7.50, "high": 7.60, "low": 7.40, "vol": 170},
        {"close": 7.55, "high": 7.65, "low": 7.45, "vol": 180},
        {"close": 7.58, "high": 7.68, "low": 7.42, "vol": 190},
    ]))

    ack = tl.handle_watch({"code": "002908", "entry_price": None, "stop_price": None},
                          now=tl.datetime(2026, 5, 14, 16, 0))

    assert "已分析并开始盯盘" in ack
    row = sqlite3.connect(tmp_path / "t.db").execute(
        "SELECT trade_date, code, name, entry_price, stop_price, target_pct FROM watchlist_dynamic WHERE code='002908'",
    ).fetchone()
    assert row[0] == "2026-05-15"
    assert row[1] == "002908"
    assert row[2] == "德生科技"
    assert row[3] > 7.58
    assert row[4] < row[3]
    assert row[5] == 5.0


def test_handle_wrong_chat_id_silent(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "12345")
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "send_message") as send, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("600519", chat_id="999", today="2026-05-14")
    p.assert_not_called()
    send.assert_not_called()
    r.assert_not_called()


def test_feishu_channel_message_replies_to_feishu(monkeypatch):
    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "oc_1")
    sent = []

    class FakeGateway:
        def send_text(self, text, *, source, channel, target, format):
            sent.append((channel, target, text, format, source))
            return Delivery(
                channel=channel,
                account_id="fake",
                conversation_id=target,
                provider_message_id="om_1",
                editable=False,
            )

        def edit_text(self, delivery, text, *, source, format):
            return delivery

        def log_inbound_start(self, message, *, db_path=None):
            return 1

        def log_inbound_finish(self, *_args, **_kwargs):
            return None

        def log_inbound_update_parsed(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(tl, "get_default_gateway", lambda: FakeGateway())

    tl.handle_channel_message(ChannelMessage(
        channel="feishu",
        account_id="cli_x",
        conversation_id="oc_1",
        sender_id="ou_1",
        message_id="om_in",
        text="/help",
    ))

    assert sent[0][0] == "feishu"
    assert sent[0][1] == "oc_1"
    assert "交易流水命令" in sent[0][2]
    assert sent[0][3] == "markdown"


def test_non_editable_channel_skips_transient_edits(monkeypatch):
    sent = []
    delivery = Delivery(
        channel="feishu",
        account_id="cli_x",
        conversation_id="oc_1",
        provider_message_id="om_ack",
        editable=False,
    )

    class FakeGateway:
        def edit_text(self, delivery, text, *, source, format):
            sent.append((text, source, format))
            return Delivery(
                channel="feishu",
                account_id="cli_x",
                conversation_id="oc_1",
                provider_message_id=f"om_{len(sent)}",
                editable=False,
            )

    monkeypatch.setattr(tl, "get_default_gateway", lambda: FakeGateway())
    tl._response_deliveries["om_ack"] = delivery

    tl.edit_message("om_ack", "🔍 600519\n\n中间流式内容")       # transient -> dropped
    tl.edit_message("om_ack", "最终卡片", final=True)            # final -> sent

    assert sent == [("最终卡片", "feishu-listener-edit", "markdown")]


def test_feishu_allowed_chat_wildcard_is_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "*")

    assert tl._is_allowed_chat("feishu", "oc_any")


def test_wecom_allowed_users_gate(monkeypatch):
    monkeypatch.setenv("WECOM_ALLOWED_USERS", "u1,u2")
    assert tl._is_allowed_chat("wecom", "u1") is True
    assert tl._is_allowed_chat("wecom", "u9") is False


def test_weixin_allowed_users_gate(monkeypatch):
    monkeypatch.delenv("WEIXIN_HOME_CHANNEL", raising=False)
    monkeypatch.setenv("WEIXIN_ALLOWED_USERS", "peer1,peer2")
    assert tl._is_allowed_chat("weixin", "peer1") is True
    assert tl._is_allowed_chat("weixin", "stranger") is False


def test_weixin_falls_back_to_home_channel(monkeypatch):
    monkeypatch.delenv("WEIXIN_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("WEIXIN_HOME_CHANNEL", "peer1")
    assert tl._is_allowed_chat("weixin", "peer1") is True
    assert tl._is_allowed_chat("weixin", "peer9") is False


def test_handle_chinese_no_hit(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("根本不存在公司名", chat_id="999", today="2026-05-14")
    p.assert_called_once()
    assert "未找到" in p.call_args.args[0]
    r.assert_not_called()


# ============================================================
# /buy /sell 交易流水命令
# ============================================================

import pytest  # noqa: E402
from datetime import datetime  # noqa: E402


def test_parse_buy_minimal():
    p = tl.parse_trade_command("/buy 600519 12.34 10 自主")
    assert p == {"side": "buy", "code": "600519", "price": 12.34,
                 "qty": 1000, "reason": "自主", "ts_override": None}


def test_parse_sell_with_time_override():
    p = tl.parse_trade_command("/sell 600519 15.0 5 止盈 @09:35")
    assert p["side"] == "sell"
    assert p["ts_override"] == "09:35"
    assert p["qty"] == 500
    assert p["reason"] == "止盈"


def test_parse_no_reason_allowed():
    p = tl.parse_trade_command("/buy 600519 12.34 10")
    assert p["reason"] is None


def test_parse_uppercase_command():
    p = tl.parse_trade_command("/BUY 600519 12.34 10")
    assert p["side"] == "buy"


def test_parse_non_trade_returns_none():
    assert tl.parse_trade_command("600519") is None
    assert tl.parse_trade_command("茅台") is None
    assert tl.parse_trade_command("") is None


def test_parse_bad_code():
    with pytest.raises(ValueError, match="代码"):
        tl.parse_trade_command("/buy 60519 12.34 10")


def test_parse_bad_price():
    with pytest.raises(ValueError, match="价格"):
        tl.parse_trade_command("/buy 600519 abc 10")
    with pytest.raises(ValueError, match="价格"):
        tl.parse_trade_command("/buy 600519 0 10")


def test_parse_bad_qty():
    with pytest.raises(ValueError, match="手数"):
        tl.parse_trade_command("/buy 600519 12.34 0")
    with pytest.raises(ValueError, match="手数"):
        tl.parse_trade_command("/buy 600519 12.34 abc")


def test_parse_invalid_reason_buy():
    with pytest.raises(ValueError, match="理由"):
        tl.parse_trade_command("/buy 600519 12.34 10 止盈")  # 止盈是 sell 理由


def test_parse_invalid_reason_sell():
    with pytest.raises(ValueError, match="理由"):
        tl.parse_trade_command("/sell 600519 12.34 10 二板接力")


def test_parse_bad_time_format():
    with pytest.raises(ValueError, match="时间"):
        tl.parse_trade_command("/buy 600519 12.34 10 自主 @9999")
    with pytest.raises(ValueError, match="时间"):
        tl.parse_trade_command("/buy 600519 12.34 10 自主 @25:00")


def test_parse_too_few_args():
    with pytest.raises(ValueError, match="格式"):
        tl.parse_trade_command("/buy 600519 12.34")


def test_build_ts_with_override():
    now = datetime(2026, 5, 14, 14, 0, 0)
    assert tl._build_ts("09:35", now=now).startswith("2026-05-14T09:35:00")


def test_build_ts_no_override():
    now = datetime(2026, 5, 14, 14, 30, 45)
    assert tl._build_ts(None, now=now) == "2026-05-14T14:30:45"


def _ensure_trades_table(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.commit()
    conn.close()


def test_record_trade_writes_row(tmp_path):
    db = tmp_path / "t.db"
    _ensure_trades_table(db)
    parsed = tl.parse_trade_command("/buy 600519 12.34 10 自主 @09:35")
    rid = tl.record_trade(parsed, source_msg_id=42, db_path=db,
                          now=datetime(2026, 5, 14, 14, 0, 0))
    assert rid >= 1
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT ts, code, side, price, qty, reason, source_msg_id "
        "FROM trades WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row[0].startswith("2026-05-14T09:35")
    assert row[1] == "600519"
    assert row[2] == "buy"
    assert row[3] == 12.34
    assert row[4] == 1000
    assert row[5] == "自主"
    assert row[6] == 42


def test_record_trade_no_source(tmp_path):
    db = tmp_path / "t.db"
    _ensure_trades_table(db)
    parsed = tl.parse_trade_command("/sell 600519 15 5 止盈")
    rid = tl.record_trade(parsed, source_msg_id=None, db_path=db)
    conn = sqlite3.connect(db)
    src = conn.execute(
        "SELECT source_msg_id FROM trades WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    assert src is None


def test_handle_buy_command_writes_and_replies(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db = tmp_path / "t.db"
    monkeypatch.setattr(tl, "TRADES_DB", db)
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("/buy 600519 12.34 10 自主", chat_id="999",
                  reply_to_msg_id=271)
    p.assert_called_once()
    ack = p.call_args.args[0]
    assert "买入" in ack and "600519" in ack and "msg_id=271" in ack
    r.assert_not_called()
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT code, side, qty, reason, source_msg_id FROM trades"
    ).fetchone()
    conn.close()
    assert row == ("600519", "buy", 1000, "自主", 271)
    holdings = tl.holdings_lib.read_holdings()
    assert len(holdings) == 1
    assert holdings[0].code == "600519"
    assert holdings[0].name == "贵州茅台"
    assert holdings[0].shares == 1000
    assert holdings[0].cost == 12.34
    assert holdings[0].source == "bot_buy"


def test_handle_sell_command_reduces_holdings(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db = tmp_path / "t.db"
    monkeypatch.setattr(tl, "TRADES_DB", db)
    tl.holdings_lib.upsert_holding(tl.holdings_lib.Holding(
        code="600519",
        name="贵州茅台",
        genre="未标记",
        cost=12.34,
        shares=1000,
        buy_date=datetime(2026, 5, 14).date(),
        source="bot_buy",
    ))
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("/sell 600519 15.0 4 止盈", chat_id="999")
    ack = p.call_args.args[0]
    assert "卖出" in ack and "剩余 600 股" in ack
    r.assert_not_called()
    holdings = tl.holdings_lib.read_holdings()
    assert len(holdings) == 1
    assert holdings[0].shares == 600


def test_handle_sell_command_clears_holdings(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db = tmp_path / "t.db"
    monkeypatch.setattr(tl, "TRADES_DB", db)
    tl.holdings_lib.upsert_holding(tl.holdings_lib.Holding(
        code="600519",
        name="贵州茅台",
        genre="未标记",
        cost=12.34,
        shares=1000,
        buy_date=datetime(2026, 5, 14).date(),
        source="bot_buy",
    ))
    with patch.object(tl, "push_reply") as p:
        tl.handle("/sell 600519 15.0 10 止盈", chat_id="999")
    ack = p.call_args.args[0]
    assert "卖出" in ack and "已清仓" in ack
    assert tl.holdings_lib.read_holdings() == []


def test_handle_invalid_trade_replies_error(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db = tmp_path / "t.db"
    monkeypatch.setattr(tl, "TRADES_DB", db)
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("/buy 600519 12.34 10 错理由", chat_id="999")
    p.assert_called_once()
    assert "❌" in p.call_args.args[0] and "理由" in p.call_args.args[0]
    r.assert_not_called()
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.close()
    assert n == 0


def test_handle_help_command(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("/help", chat_id="999")
    p.assert_called_once()
    msg = p.call_args.args[0]
    assert "/buy" in msg and "/sell" in msg and "二板接力" in msg
    r.assert_not_called()


def test_handle_bare_buy_shows_usage(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("/buy", chat_id="999")
    p.assert_called_once()
    msg = p.call_args.args[0]
    assert "买入" in msg and "二板接力" in msg
    r.assert_not_called()


def test_handle_bare_sell_shows_usage(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill_streaming") as r:
        tl.handle("/sell", chat_id="999")
    msg = p.call_args.args[0]
    assert "卖出" in msg and "止盈" in msg
    r.assert_not_called()


def test_handle_bad_reason_lists_options(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db = tmp_path / "t.db"
    monkeypatch.setattr(tl, "TRADES_DB", db)
    with patch.object(tl, "push_reply") as p:
        tl.handle("/buy 600519 12.34 10 瞎写", chat_id="999")
    msg = p.call_args.args[0]
    for r in tl.BUY_REASONS:
        assert r in msg


def test_handle_trade_does_not_block_query(tmp_path, monkeypatch):
    """非 /buy /sell 开头，走原 query 流程。"""
    _setup(monkeypatch, tmp_path)
    with patch.object(tl, "send_message", return_value=1), \
         patch.object(tl, "edit_message"), \
         patch.object(tl, "run_skill_streaming",
                      return_value="📊 fake") as r:
        tl.handle("600519", chat_id="999")
    r.assert_called_once()

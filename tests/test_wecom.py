from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stock_codex.channels import ChannelGateway
from stock_codex.channels.wecom import WeComAdapter

ROOT = Path(__file__).resolve().parents[1]


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.commit()
    conn.close()
    return path


def _adapter() -> WeComAdapter:
    return WeComAdapter(bot_id="bot1", secret="sec", default_conversation_id="home1")


def test_normalize_single_chat_callback():
    msg = _adapter().normalize_event({
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {
            "msgid": "m1",
            "aibotid": "bot1",
            "chattype": "single",
            "from": {"userid": "u1"},
            "msgtype": "text",
            "text": {"content": "600519"},
        },
    })
    assert msg is not None
    assert msg.channel == "wecom"
    assert msg.sender_id == "u1"
    assert msg.message_id == "m1"
    assert msg.text == "600519"
    # single chat omits chatid -> conversation keys off the user
    assert msg.conversation_id == "u1"
    assert msg.is_direct_message is True


def test_normalize_group_callback_uses_chatid():
    msg = _adapter().normalize_event({
        "cmd": "aibot_msg_callback",
        "body": {
            "msgid": "m2",
            "chatid": "g9",
            "chattype": "group",
            "from": {"userid": "u2"},
            "msgtype": "text",
            "text": {"content": "/ask 光伏"},
        },
    })
    assert msg is not None
    assert msg.conversation_id == "g9"
    assert msg.is_direct_message is False
    assert msg.raw["chat_type"] == "group"


def test_normalize_ignores_non_text_and_other_cmds():
    a = _adapter()
    assert a.normalize_event({"cmd": "pong"}) is None
    assert a.normalize_event({
        "cmd": "aibot_msg_callback",
        "body": {"msgid": "m", "msgtype": "image", "from": {"userid": "u"}},
    }) is None


def test_send_frame_markdown_and_text():
    a = _adapter()
    md = a.send_frame("g9", "**hi**", format="markdown", req_id="r1")
    assert md == {
        "cmd": "aibot_send_msg",
        "headers": {"req_id": "r1"},
        "body": {"chatid": "g9", "msgtype": "markdown", "markdown": {"content": "**hi**"}},
    }
    plain = a.send_frame("g9", "hi", format="plain", req_id="r2")
    assert plain["body"] == {"chatid": "g9", "msgtype": "text", "text": {"content": "hi"}}


def test_subscribe_frame_carries_bot_credentials():
    frame = _adapter().subscribe_frame(req_id="s1")
    assert frame == {
        "cmd": "aibot_subscribe",
        "headers": {"req_id": "s1"},
        "body": {"botId": "bot1", "secret": "sec"},
    }


def test_direct_send_is_blocked_use_gateway():
    with pytest.raises(Exception):
        _adapter().send_text("g9", "hi")


def test_gateway_routes_wecom_through_outbox(tmp_path):
    db = _db(tmp_path / "t.db")
    gateway = ChannelGateway({"wecom": _adapter()}, default_channel="wecom", db_path=db)

    delivery = gateway.send_text("card body", source="stock-premarket", target="g9", format="markdown")

    assert delivery.raw["queued"] is True
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT channel, target, text, format, status FROM channel_outbox"
        ).fetchone()
    assert row == ("wecom", "g9", "card body", "markdown", "pending")

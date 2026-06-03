from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stock_codex.channels import ChannelGateway
from stock_codex.channels.weixin import WeixinAdapter

ROOT = Path(__file__).resolve().parents[1]


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.commit()
    conn.close()
    return path


def _adapter() -> WeixinAdapter:
    return WeixinAdapter(account_id="bot@im.bot", token="tok", default_conversation_id="home@im.wechat")


def test_normalize_user_text_message():
    msg = _adapter().normalize_event({
        "from_user_id": "u1@im.wechat",
        "to_user_id": "bot@im.bot",
        "message_type": 1,
        "message_state": 2,
        "context_token": "CTX-123",
        "svr_id": "s99",
        "item_list": [{"type": 1, "text_item": {"text": "600519"}}],
    })
    assert msg is not None
    assert msg.channel == "weixin"
    assert msg.sender_id == "u1@im.wechat"
    assert msg.conversation_id == "u1@im.wechat"   # 1v1 keyed by peer
    assert msg.message_id == "s99"
    assert msg.text == "600519"
    assert msg.is_direct_message is True
    assert msg.raw["context_token"] == "CTX-123"


def test_normalize_ignores_bot_echo_and_nontext():
    a = _adapter()
    # bot's own message
    assert a.normalize_event({
        "from_user_id": "bot@im.bot", "message_type": 2,
        "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
    }) is None
    # non-text item
    assert a.normalize_event({
        "from_user_id": "u1", "message_type": 1,
        "item_list": [{"type": 2}],
    }) is None


def test_message_id_falls_back_to_hash_when_no_server_id():
    a = _adapter()
    m1 = a.normalize_event({
        "from_user_id": "u1", "message_type": 1, "context_token": "c",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    })
    m2 = a.normalize_event({
        "from_user_id": "u1", "message_type": 1, "context_token": "c",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    })
    # deterministic + stable across identical payloads (dedupe-friendly)
    assert m1.message_id == m2.message_id
    assert len(m1.message_id) == 16


def test_send_payload_echoes_context_token():
    payload = _adapter().send_payload("u1@im.wechat", "卡片正文", context_token="CTX-123")
    assert payload == {
        "msg": {
            "to_user_id": "u1@im.wechat",
            "message_type": 2,
            "message_state": 2,
            "context_token": "CTX-123",
            "item_list": [{"type": 1, "text_item": {"text": "卡片正文"}}],
        }
    }


def test_auth_headers_carry_bearer_token():
    headers = _adapter().auth_headers()
    assert headers["Authorization"] == "Bearer tok"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert headers["X-WECHAT-UIN"]  # present, random per call


def test_direct_send_is_blocked_use_gateway():
    with pytest.raises(Exception):
        _adapter().send_text("u1", "hi")


def test_gateway_routes_weixin_through_outbox(tmp_path):
    db = _db(tmp_path / "t.db")
    gateway = ChannelGateway({"weixin": _adapter()}, default_channel="weixin", db_path=db)

    delivery = gateway.send_text("card", source="stock-premarket", target="u1@im.wechat", format="markdown")

    assert delivery.raw["queued"] is True
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT channel, target, status FROM channel_outbox").fetchone()
    assert row == ("weixin", "u1@im.wechat", "pending")

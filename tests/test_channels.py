from __future__ import annotations

import sqlite3
from pathlib import Path

from stock_codex.channels import (
    ChannelGateway,
    ChannelMessage,
    Delivery,
    FeishuAdapter,
    MockAdapter,
)
from stock_codex.infra import notify


ROOT = Path(__file__).resolve().parents[1]


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.commit()
    conn.close()
    return path


def test_channel_message_exposes_satori_projection():
    msg = ChannelMessage(
        channel="feishu",
        account_id="bot",
        conversation_id="chat-1",
        sender_id="user-1",
        message_id="42",
        text="/ask 光伏",
        raw={"x": 1},
    )

    assert msg.dedupe_key() == "feishu:bot:chat-1:42"
    assert msg.to_satori_dict()["platform"] == "feishu"
    assert msg.to_satori_dict()["channel_id"] == "chat-1"
    assert msg.to_satori_dict()["content"] == "/ask 光伏"


def test_gateway_send_logs_channel_outbound(tmp_path):
    db = _db(tmp_path / "t.db")
    adapter = MockAdapter()
    gateway = ChannelGateway({"mock": adapter}, default_channel="mock", db_path=db)

    delivery = gateway.send_text("hello", source="unit", format="plain")

    assert delivery.provider_message_id == "1"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT channel, conversation_id, provider_msg_id, source, text, success "
            "FROM channel_outbound_log"
        ).fetchone()
    assert row == ("mock", "mock-conversation", "1", "unit", "hello", 1)


def test_gateway_log_failure_does_not_mask_sent_delivery(tmp_path):
    bad_db_path = tmp_path / "not-a-db"
    bad_db_path.mkdir()
    adapter = MockAdapter()
    gateway = ChannelGateway({"mock": adapter}, default_channel="mock", db_path=bad_db_path)

    delivery = gateway.send_text("hello", source="unit")

    assert delivery.provider_message_id == "1"
    assert adapter.sent[0]["text"] == "hello"


def test_gateway_edit_falls_back_to_new_message_when_adapter_cannot_edit(tmp_path):
    db = _db(tmp_path / "t.db")
    adapter = MockAdapter(edit_text=False)
    gateway = ChannelGateway({"mock": adapter}, default_channel="mock", db_path=db)
    first = gateway.send_text("loading", source="unit")

    second = gateway.edit_text(first, "final", source="unit")

    assert second.provider_message_id == "2"
    assert adapter.edits == []
    assert adapter.sent[-1]["text"] == "final"


def test_gateway_inbound_log_round_trip(tmp_path):
    db = _db(tmp_path / "t.db")
    adapter = MockAdapter()
    gateway = ChannelGateway({"mock": adapter}, default_channel="mock", db_path=db)
    msg = ChannelMessage(
        channel="mock",
        account_id="bot",
        conversation_id="c1",
        sender_id="u1",
        message_id="m1",
        event_id="e1",
        text="/ask 光伏",
    )

    inbound_id = gateway.log_inbound_start(msg)
    gateway.log_inbound_update_parsed(
        inbound_id,
        parsed_command="/ask",
        parsed_intent="sector",
        parsed_payload={"sector": "光伏"},
    )
    gateway.log_inbound_finish(
        inbound_id,
        response=Delivery(
            channel="mock",
            account_id="bot",
            conversation_id="c1",
            provider_message_id="r1",
        ),
        status="ok",
        duration_ms=123,
    )

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT channel, provider_msg_id, dedupe_key, parsed_command, "
            "parsed_intent, parsed_payload, response_msg_id, handler_status, duration_ms "
            "FROM channel_inbound_log"
        ).fetchone()
    assert row[0:5] == ("mock", "m1", "mock:bot:c1:e1", "/ask", "sector")
    assert row[5] == '{"sector": "光伏"}'
    assert row[6:] == ("r1", "ok", 123)


def test_feishu_send_text_fetches_token_and_posts_message(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return FakeResponse({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        return FakeResponse({"code": 0, "data": {"message_id": "om_1"}})

    monkeypatch.setattr("stock_codex.channels.core.requests.post", fake_post)
    adapter = FeishuAdapter(
        app_id="cli_x",
        app_secret="secret",
        default_conversation_id="oc_1",
        api_base="https://open.feishu.cn/open-apis",
    )

    delivery = adapter.send_text("oc_1", "hello 飞书")

    assert delivery.channel == "feishu"
    assert delivery.provider_message_id == "om_1"
    assert calls[1][1]["params"] == {"receive_id_type": "chat_id"}
    assert calls[1][1]["headers"]["Authorization"] == "Bearer t-1"
    assert calls[1][1]["json"]["receive_id"] == "oc_1"
    assert calls[1][1]["json"]["content"] == '{"text": "hello 飞书"}'


def test_feishu_send_text_can_target_open_id(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return FakeResponse({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        return FakeResponse({"code": 0, "data": {"message_id": "om_1"}})

    monkeypatch.setattr("stock_codex.channels.core.requests.post", fake_post)
    adapter = FeishuAdapter(app_id="cli_x", app_secret="secret", default_conversation_id="oc_1")

    delivery = adapter.send_text("open_id:ou_1", "hello")

    assert delivery.provider_message_id == "om_1"
    assert calls[1][1]["params"] == {"receive_id_type": "open_id"}
    assert calls[1][1]["json"]["receive_id"] == "ou_1"


def test_feishu_send_text_can_render_markdown_card(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return FakeResponse({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        return FakeResponse({"code": 0, "data": {"message_id": "om_1"}})

    monkeypatch.setattr("stock_codex.channels.core.requests.post", fake_post)
    adapter = FeishuAdapter(app_id="cli_x", app_secret="secret", default_conversation_id="oc_1")

    adapter.send_text("oc_1", "**hello**", format="markdown")

    assert calls[1][1]["json"]["msg_type"] == "interactive"
    content = calls[1][1]["json"]["content"]
    assert '"tag": "markdown"' in content
    assert "**hello**" in content
    # card disabled by default: simple markdown element, no header
    assert "header" not in content


def test_feishu_card_renders_colored_header_when_enabled(monkeypatch):
    import json as _json

    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return FakeResponse({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        return FakeResponse({"code": 0, "data": {"message_id": "om_1"}})

    monkeypatch.setattr("stock_codex.channels.core.requests.post", fake_post)
    adapter = FeishuAdapter(
        app_id="cli_x", app_secret="secret", default_conversation_id="oc_1",
        card_enabled=True,
    )

    adapter.send_text("oc_1", "📊 600519 贵州茅台\n买点 1620 突破\n仓位 ≤15%", format="markdown")

    body = _json.loads(calls[1][1]["json"]["content"])
    assert body["header"]["title"]["content"] == "📊 600519 贵州茅台"
    assert body["header"]["template"] == "green"  # 含“买点/突破”
    assert body["elements"][0]["tag"] == "markdown"
    assert "买点 1620 突破" in body["elements"][0]["content"]
    # 标题行不重复进正文
    assert "📊 600519 贵州茅台" not in body["elements"][0]["content"]


def test_feishu_card_header_color_bearish():
    adapter = FeishuAdapter(app_id="x", app_secret="y", default_conversation_id="oc", card_enabled=True)
    import json as _json
    body = _json.loads(adapter._render_card("⚠️ 600519\n跌破止损 1580"))
    assert body["header"]["template"] == "red"


def test_feishu_normalize_receive_event_strips_bot_mention():
    adapter = FeishuAdapter(app_id="cli_x", app_secret="secret", default_conversation_id="oc_1")
    msg = adapter.normalize_event({
        "schema": "2.0",
        "header": {
            "event_id": "evt_1",
            "event_type": "im.message.receive_v1",
            "app_id": "cli_x",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "msg_type": "text",
                "content": '{"text":"@_user_1 /ask 光伏"}',
                "mentions": [{"key": "@_user_1", "id": "ou_bot"}],
            },
        },
    })

    assert msg is not None
    assert msg.channel == "feishu"
    assert msg.account_id == "cli_x"
    assert msg.conversation_id == "oc_1"
    assert msg.sender_id == "ou_1"
    assert msg.message_id == "om_1"
    assert msg.event_id == "om_1"
    assert msg.text == "/ask 光伏"


def test_notify_push_uses_gateway_and_keeps_push_log_compat(tmp_path, monkeypatch):
    db = _db(tmp_path / "t.db")
    adapter = MockAdapter()
    gateway = ChannelGateway({"mock": adapter}, default_channel="mock", db_path=db)
    monkeypatch.setattr(notify, "DB", db)
    monkeypatch.setattr(notify, "CHAT_ID", "legacy-chat")
    monkeypatch.setattr(notify, "get_default_gateway", lambda: gateway)
    monkeypatch.delenv("CHANNELS_NOTIFY", raising=False)

    result = notify.push("**hello**", source="unit")

    assert result["result"]["message_id"] == "1"
    assert adapter.sent[0]["format"] == "markdown"
    assert adapter.sent[0]["text"] == "**hello**"
    with sqlite3.connect(db) as conn:
        push_row = conn.execute("SELECT source, chat_id, msg_id, text, success FROM push_log").fetchone()
        channel_row = conn.execute(
            "SELECT channel, source, provider_msg_id, text FROM channel_outbound_log"
        ).fetchone()
    assert push_row == ("unit", "legacy-chat", 1, "**hello**", 1)
    assert channel_row == ("mock", "unit", "1", "**hello**")


def test_notify_push_raw_sends_plain_text(tmp_path, monkeypatch):
    db = _db(tmp_path / "t.db")
    adapter = MockAdapter()
    gateway = ChannelGateway({"mock": adapter}, default_channel="mock", db_path=db)
    monkeypatch.setattr(notify, "DB", db)
    monkeypatch.setattr(notify, "get_default_gateway", lambda: gateway)
    monkeypatch.delenv("CHANNELS_NOTIFY", raising=False)

    notify.push("✅ 纯文本", source="unit", raw=True)

    assert adapter.sent[0]["format"] == "plain"
    assert adapter.sent[0]["text"] == "✅ 纯文本"


def test_notify_push_fans_out_to_configured_channels(tmp_path, monkeypatch):
    db = _db(tmp_path / "t.db")
    adapter = MockAdapter()
    gateway = ChannelGateway({"weixin": adapter, "feishu": adapter}, default_channel="feishu", db_path=db)
    monkeypatch.setattr(notify, "DB", db)
    monkeypatch.setattr(notify, "CHAT_ID", "legacy-chat")
    monkeypatch.setattr(notify, "get_default_gateway", lambda: gateway)
    monkeypatch.setenv("CHANNELS_NOTIFY", "weixin,feishu")

    result = notify.push("**hello**", source="unit")

    assert result["result"]["message_id"] == "1"
    assert adapter.sent[0]["format"] == "markdown"
    assert adapter.sent[1]["format"] == "markdown"
    assert adapter.sent[1]["text"] == "**hello**"

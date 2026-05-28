from __future__ import annotations

from types import SimpleNamespace

from stock_codex.apps import channel_listener
from stock_codex.channels import FeishuAdapter


def test_enabled_channels_defaults_to_telegram(monkeypatch):
    monkeypatch.setattr(channel_listener, "load_env_file", lambda: None)
    monkeypatch.delenv("CHANNELS_ENABLED", raising=False)
    monkeypatch.delenv("CHANNEL_DEFAULT", raising=False)
    monkeypatch.delenv("FEISHU_ENABLED", raising=False)

    assert channel_listener.enabled_channels() == {"telegram"}


def test_enabled_channels_parses_env(monkeypatch):
    monkeypatch.setattr(channel_listener, "load_env_file", lambda: None)
    monkeypatch.setenv("CHANNELS_ENABLED", "telegram, feishu")

    assert channel_listener.enabled_channels() == {"telegram", "feishu"}


def test_feishu_message_from_sdk_event_converts_to_channel_message():
    adapter = FeishuAdapter(app_id="cli_x", app_secret="secret", default_conversation_id="oc_1")
    data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_1", user_id=None, union_id=None)
            ),
            message=SimpleNamespace(
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                msg_type="text",
                content='{"text":"@_user_1 /ask 光伏"}',
                mentions=[SimpleNamespace(
                    key="@_user_1",
                    id=SimpleNamespace(open_id="ou_bot", user_id=None, union_id=None),
                    name="bot",
                )],
                thread_id=None,
                root_id=None,
            ),
        )
    )

    msg = channel_listener.feishu_message_from_sdk_event(data, adapter)

    assert msg is not None
    assert msg.channel == "feishu"
    assert msg.conversation_id == "oc_1"
    assert msg.sender_id == "ou_1"
    assert msg.text == "/ask 光伏"
    assert msg.raw["chat_type"] == "group"


def test_feishu_message_from_sdk_event_accepts_lark_sdk_message_type_field():
    adapter = FeishuAdapter(app_id="cli_x", app_secret="secret", default_conversation_id="oc_1")
    data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_1", user_id=None, union_id=None),
                sender_type="user",
            ),
            message=SimpleNamespace(
                message_id="om_2",
                chat_id="oc_1",
                chat_type="p2p",
                message_type="text",
                content={"text": "/help"},
                mentions=[],
                thread_id=None,
                root_id=None,
            ),
        )
    )

    msg = channel_listener.feishu_message_from_sdk_event(data, adapter)

    assert msg is not None
    assert msg.message_id == "om_2"
    assert msg.text == "/help"
    assert msg.is_direct_message


def test_feishu_message_from_sdk_event_ignores_non_text():
    adapter = FeishuAdapter(app_id="cli_x", app_secret="secret", default_conversation_id="oc_1")
    data = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
            message=SimpleNamespace(msg_type="image"),
        )
    )

    assert channel_listener.feishu_message_from_sdk_event(data, adapter) is None


def test_feishu_policy_requires_group_mention(monkeypatch):
    monkeypatch.setattr(channel_listener, "load_env_file", lambda: None)
    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "oc_1")
    monkeypatch.setenv("FEISHU_REQUIRE_MENTION", "true")
    policy = channel_listener.FeishuPolicy.from_env()
    msg = _feishu_msg(raw={"chat_type": "group", "mentions": []})

    allowed, reason = policy.allows(msg)

    assert allowed is False
    assert reason == "group message without bot mention"


def test_feishu_policy_allows_direct_message_without_mention(monkeypatch):
    monkeypatch.setattr(channel_listener, "load_env_file", lambda: None)
    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "oc_1")
    policy = channel_listener.FeishuPolicy.from_env()
    msg = _feishu_msg(raw={"chat_type": "p2p", "mentions": []})

    allowed, reason = policy.allows(msg)

    assert allowed is True
    assert reason is None


def test_persistent_deduper_survives_restart(tmp_path):
    path = tmp_path / "seen.json"
    first = channel_listener.PersistentDeduper(path)

    assert first.seen_or_mark("k1") is False
    assert first.seen_or_mark("k1") is True

    second = channel_listener.PersistentDeduper(path)
    assert second.seen_or_mark("k1") is True


def test_gateway_runtime_submit_is_non_blocking_and_serial_per_chat(tmp_path, monkeypatch):
    calls = []

    def fake_handle(message):
        calls.append(message.message_id)

    monkeypatch.setattr(channel_listener.tg_listener, "handle_channel_message", fake_handle)
    runtime = channel_listener.GatewayRuntime(
        policy=channel_listener.FeishuPolicy(
            allowed_chat_ids=frozenset({"oc_1"}),
            allowed_user_ids=frozenset(),
            require_mention=False,
        ),
        deduper=channel_listener.PersistentDeduper(tmp_path / "seen.json"),
        state_file=tmp_path / "state.json",
    )

    assert runtime.submit(_feishu_msg(message_id="om_1")) is True
    assert runtime.submit(_feishu_msg(message_id="om_2")) is True
    runtime._queues["oc_1"].join()

    assert calls == ["om_1", "om_2"]


def test_gateway_runtime_dedupes_before_worker(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(channel_listener.tg_listener, "handle_channel_message", lambda message: calls.append(message))
    runtime = channel_listener.GatewayRuntime(
        policy=channel_listener.FeishuPolicy(
            allowed_chat_ids=frozenset({"oc_1"}),
            allowed_user_ids=frozenset(),
            require_mention=False,
        ),
        deduper=channel_listener.PersistentDeduper(tmp_path / "seen.json"),
        state_file=tmp_path / "state.json",
    )

    assert runtime.submit(_feishu_msg(message_id="om_1")) is True
    assert runtime.submit(_feishu_msg(message_id="om_1")) is False
    runtime._queues["oc_1"].join()

    assert len(calls) == 1


def test_main_routes_telegram_through_gateway_runtime(monkeypatch):
    calls = []

    class FakeRuntime:
        def start(self, *, channels):
            calls.append(("start", channels))

    monkeypatch.setattr(channel_listener, "enabled_channels", lambda: {"telegram"})
    monkeypatch.setattr(channel_listener, "_acquire_gateway_lock", lambda: object())
    monkeypatch.setattr(channel_listener, "GatewayRuntime", lambda: FakeRuntime())
    monkeypatch.setattr(channel_listener, "run_telegram_poll", lambda *, runtime: calls.append(("telegram", runtime)))
    monkeypatch.setattr(channel_listener.tg_listener, "main", lambda: calls.append(("legacy", None)))

    channel_listener.main()

    assert calls[0] == ("start", {"telegram"})
    assert calls[1][0] == "telegram"
    assert ("legacy", None) not in calls


def test_gateway_runtime_start_writes_json_state(tmp_path):
    runtime = channel_listener.GatewayRuntime(
        policy=channel_listener.FeishuPolicy(
            allowed_chat_ids=frozenset({"oc_1"}),
            allowed_user_ids=frozenset(),
            require_mention=False,
        ),
        deduper=channel_listener.PersistentDeduper(tmp_path / "seen.json"),
        state_file=tmp_path / "state.json",
    )

    runtime.start(channels={"telegram"})

    assert '"channels": [\n    "telegram"\n  ]' in (tmp_path / "state.json").read_text()


def _feishu_msg(*, message_id: str = "om_1", raw: dict | None = None):
    return channel_listener.ChannelMessage(
        channel="feishu",
        account_id="cli_x",
        conversation_id="oc_1",
        sender_id="ou_1",
        message_id=message_id,
        event_id=message_id,
        text="/help",
        raw=raw or {"chat_type": "p2p", "mentions": []},
    )

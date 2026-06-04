"""Channel gateway core types — platform-neutral.

These models form the contract every platform adapter implements. Keeping
them free of any platform/SDK import lets adapters (feishu/weixin) and the
gateway depend on a single small surface without cycles.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from stock_codex.paths import ENV_FILE


class ChannelError(RuntimeError):
    pass


@dataclass(frozen=True)
class Capabilities:
    send_text: bool = True
    edit_text: bool = False
    markdown: bool = False
    html: bool = False
    card: bool = False
    streaming: bool = False
    # connection_bound: outbound must go through a persistent connection held
    # by the listener process (e.g. WeChat iLink), so other
    # processes enqueue to channel_outbox instead of sending directly. Stateless
    # REST adapters (feishu) leave this False.
    connection_bound: bool = False


@dataclass(frozen=True)
class ChannelMessage:
    channel: str
    account_id: str
    conversation_id: str
    sender_id: str
    message_id: str
    text: str
    thread_id: str | None = None
    event_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def dedupe_key(self) -> str:
        event = self.event_id or self.message_id
        return f"{self.channel}:{self.account_id}:{self.conversation_id}:{event}"

    @property
    def is_direct_message(self) -> bool:
        return str(self.raw.get("chat_type") or "").lower() in {"p2p", "private", "direct", "dm"}

    @property
    def is_from_bot(self) -> bool:
        return bool(self.raw.get("is_bot"))

    def to_satori_dict(self) -> dict[str, Any]:
        """Return a Satori-shaped projection for future adapter compatibility."""
        return {
            "platform": self.channel,
            "self_id": self.account_id,
            "channel_id": self.conversation_id,
            "user_id": self.sender_id,
            "message_id": self.message_id,
            "content": self.text,
            "original": self.raw,
        }


@dataclass(frozen=True)
class Delivery:
    channel: str
    account_id: str
    conversation_id: str
    provider_message_id: str
    thread_id: str | None = None
    editable: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class ChannelAdapter(Protocol):
    channel: str
    account_id: str
    capabilities: Capabilities

    def default_target(self) -> str:
        ...

    def send_text(self, target: str, text: str, *, format: str = "plain") -> Delivery:
        ...

    def edit_text(self, delivery: Delivery, text: str, *, format: str = "plain") -> bool:
        ...


def loads_json_object(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

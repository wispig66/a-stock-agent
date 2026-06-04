"""WeChat personal (iLink Bot API) adapter.

Tencent's official personal-WeChat bot gateway (``ilinkai.weixin.qq.com``).
HTTP long-polling, 1v1 only (the iLink identity cannot join ordinary groups).

Endpoints (see openclaw-weixin reference):
  GET  /ilink/bot/get_bot_qrcode?bot_type=3      -> {qrcode, qrcode_img_content}
  GET  /ilink/bot/get_qrcode_status?qrcode=...   -> {status, bot_token, baseurl}
  POST /ilink/bot/getupdates  {get_updates_buf, base_info} -> {ret, msgs[], get_updates_buf, longpolling_timeout_ms}
  POST /ilink/bot/sendmessage {msg:{...}}

Auth headers on every request:
  AuthorizationType: ilink_bot_token
  X-WECHAT-UIN: base64(random uint32)   # anti-replay, fresh each request
  Authorization: Bearer <bot_token>

Outbound must echo the peer's ``context_token`` (from the inbound message) or
the reply won't land in the right conversation. This module holds the pure,
unit-testable pieces; the login + long-poll loop lives in
``apps.channel_listener.run_weixin_listener`` (it owns the context-token store).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any

from stock_codex.channels.base import Capabilities, ChannelError, ChannelMessage, Delivery

WEIXIN_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"

# iLink item types
_ITEM_TEXT = 1
# message_type
_MSG_FROM_USER = 1
_MSG_FROM_BOT = 2
# message_state
_STATE_FINISH = 2


class WeixinAdapter:
    channel = "weixin"
    capabilities = Capabilities(
        send_text=True,
        edit_text=False,
        markdown=True,   # iLink clients render markdown directly
        html=False,
        card=False,
        streaming=False,
        connection_bound=True,
    )

    def __init__(
        self,
        *,
        account_id: str,
        token: str,
        default_conversation_id: str,
        base_url: str = WEIXIN_BASE_URL,
    ) -> None:
        self.account_id = account_id or "default"
        self.token = token
        self.default_conversation_id = default_conversation_id
        self.base_url = base_url.rstrip("/")

    def default_target(self) -> str:
        if not self.default_conversation_id:
            raise ChannelError("WEIXIN_HOME_CHANNEL is not configured")
        return self.default_conversation_id

    def send_text(self, target: str, text: str, *, format: str = "plain") -> Delivery:
        raise ChannelError(
            "WeChat iLink is connection-bound; send via ChannelGateway (routed through the outbox)"
        )

    def edit_text(self, delivery: Delivery, text: str, *, format: str = "plain") -> bool:
        return False

    # --- pure helpers (no socket) -------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        uin = base64.b64encode(str(secrets.randbits(32)).encode()).decode()
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin,
            "Authorization": f"Bearer {self.token}",
        }

    def getupdates_payload(self, buf: str = "") -> dict[str, Any]:
        return {"get_updates_buf": buf, "base_info": {"channel_version": CHANNEL_VERSION}}

    @staticmethod
    def new_client_id() -> str:
        # Outbound dedupe id the relay expects on every send (mirrors the
        # reference client's ``wechat-ilink-<rand>``).
        return "wechat-ilink-" + secrets.token_hex(12)

    def send_payload(
        self,
        to_user_id: str,
        text: str,
        *,
        context_token: str,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        # The iLink relay silently drops sends missing ``base_info`` or
        # ``client_id`` (200 + empty ``{}``, no error). ``from_user_id`` is
        # echoed empty per the reference client; routing is by context_token.
        return {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id or self.new_client_id(),
                "message_type": _MSG_FROM_BOT,
                "message_state": _STATE_FINISH,
                "context_token": context_token or "",
                "item_list": [{"type": _ITEM_TEXT, "text_item": {"text": text}}],
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }

    @staticmethod
    def _message_id(msg: dict[str, Any], sender: str, text: str, context_token: str) -> str:
        # iLink inbound carries a top-level int ``message_id``; older/other shapes
        # may use these alternates. Fall back to a stable hash only if none exist.
        for key in ("message_id", "svr_id", "new_msg_id", "client_msg_id", "msgid", "msg_id"):
            val = msg.get(key)
            if val:
                return str(val)
        digest = hashlib.sha1(f"{sender}|{context_token}|{text}".encode("utf-8")).hexdigest()
        return digest[:16]

    def normalize_event(self, msg: dict[str, Any]) -> ChannelMessage | None:
        if not isinstance(msg, dict):
            return None
        if msg.get("message_type") != _MSG_FROM_USER:
            return None  # ignore bot/echo messages
        text = ""
        for item in msg.get("item_list") or []:
            if item.get("type") == _ITEM_TEXT:
                text = str((item.get("text_item") or {}).get("text") or "").strip()
                if text:
                    break
        if not text:
            return None  # non-text (image/voice/file) not handled yet
        sender = str(msg.get("from_user_id") or "")
        if not sender:
            return None
        context_token = str(msg.get("context_token") or "")
        message_id = self._message_id(msg, sender, text, context_token)
        return ChannelMessage(
            channel=self.channel,
            account_id=str(msg.get("to_user_id") or self.account_id),
            conversation_id=sender,   # 1v1: keyed by the peer
            sender_id=sender,
            message_id=message_id,
            event_id=message_id,
            text=text,
            raw={**msg, "chat_type": "private", "context_token": context_token},
        )

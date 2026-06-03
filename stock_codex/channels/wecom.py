"""WeCom (企业微信) smart-bot adapter — AI Bot long-connection protocol.

Frame schemas follow the official WecomTeam aibot SDK over
``wss://openws.work.weixin.qq.com``:

  subscribe : {cmd:"aibot_subscribe", headers:{req_id}, body:{botId, secret}}
  heartbeat : {cmd:"ping"}  -> server replies {cmd:"pong"}
  inbound   : {cmd:"aibot_msg_callback", headers:{req_id},
               body:{msgid, aibotid, chatid?, chattype:"single"|"group",
                     from:{userid}, msgtype:"text", text:{content}}}
  outbound  : {cmd:"aibot_send_msg", headers:{req_id},
               body:{chatid, msgtype:"markdown", markdown:{content}}}

This module holds only the platform-neutral pieces (adapter contract + pure
frame builders/parsers) so they are unit-testable without a socket. The live
WebSocket loop lives in ``apps.channel_listener.run_wecom_listener``.

Outbound is connection-bound: ``send_text`` must never be called directly —
``ChannelGateway`` routes WeCom through the outbox, and the listener's drain
sends the frame over the live connection.
"""
from __future__ import annotations

import uuid
from typing import Any

from stock_codex.channels.base import Capabilities, ChannelError, ChannelMessage, Delivery

WECOM_WS_URL = "wss://openws.work.weixin.qq.com"


def new_req_id(prefix: str = "req") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class WeComAdapter:
    channel = "wecom"
    capabilities = Capabilities(
        send_text=True,
        edit_text=False,
        markdown=True,
        html=False,
        card=True,
        streaming=False,
        connection_bound=True,
    )

    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        default_conversation_id: str,
        ws_url: str = WECOM_WS_URL,
        account_id: str | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.default_conversation_id = default_conversation_id
        self.ws_url = ws_url
        self.account_id = account_id or bot_id or "default"

    def default_target(self) -> str:
        if not self.default_conversation_id:
            raise ChannelError("WECOM_HOME_CHANNEL is not configured")
        return self.default_conversation_id

    def send_text(self, target: str, text: str, *, format: str = "plain") -> Delivery:
        # Connection-bound: the gateway enqueues to the outbox instead of calling
        # this. Direct use means the caller bypassed the gateway routing.
        raise ChannelError(
            "WeCom is connection-bound; send via ChannelGateway (routed through the outbox)"
        )

    def edit_text(self, delivery: Delivery, text: str, *, format: str = "plain") -> bool:
        return False

    # --- pure frame helpers (no socket) -------------------------------------

    def subscribe_frame(self, req_id: str | None = None) -> dict[str, Any]:
        return {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id or new_req_id("sub")},
            "body": {"botId": self.bot_id, "secret": self.secret},
        }

    def send_frame(self, target: str, text: str, *, format: str = "markdown",
                   req_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any]
        if format in {"markdown", "lark_md", "card", "interactive"}:
            body = {"chatid": target, "msgtype": "markdown", "markdown": {"content": text}}
        else:
            body = {"chatid": target, "msgtype": "text", "text": {"content": text}}
        return {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": req_id or new_req_id("send")},
            "body": body,
        }

    def normalize_event(self, frame: dict[str, Any]) -> ChannelMessage | None:
        if frame.get("cmd") != "aibot_msg_callback":
            return None
        body = frame.get("body") or {}
        if body.get("msgtype") != "text":
            return None
        content = str((body.get("text") or {}).get("content") or "").strip()
        if not content:
            return None
        msgid = str(body.get("msgid") or "")
        if not msgid:
            return None
        userid = str((body.get("from") or {}).get("userid") or "")
        chatid = str(body.get("chatid") or "")
        chattype = str(body.get("chattype") or "").lower()
        # Single chat may omit chatid; reply/target then keys off the user id.
        conversation_id = chatid or userid
        return ChannelMessage(
            channel=self.channel,
            account_id=str(body.get("aibotid") or self.account_id),
            conversation_id=conversation_id,
            sender_id=userid,
            message_id=msgid,
            event_id=msgid,
            text=content,
            raw={
                **frame,
                "chat_type": "private" if chattype == "single" else "group",
            },
        )

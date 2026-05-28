from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import requests

from stock_codex.infra.db import connect_close
from stock_codex.paths import DB_FILE, ENV_FILE


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


class TelegramAdapter:
    channel = "telegram"
    capabilities = Capabilities(
        send_text=True,
        edit_text=True,
        markdown=False,
        html=True,
        card=False,
        streaming=True,
    )

    def __init__(
        self,
        *,
        token: str,
        default_conversation_id: str,
        account_id: str = "default",
        api_base: str | None = None,
        timeout: int = 10,
    ) -> None:
        self.token = token
        self.default_conversation_id = default_conversation_id
        self.account_id = account_id
        self.api_base = api_base or f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def default_target(self) -> str:
        if not self.default_conversation_id:
            raise ChannelError("Telegram default conversation is not configured")
        return self.default_conversation_id

    def _safe_error_text(self, error: object) -> str:
        text = str(error)
        if self.token:
            text = text.replace(self.token, "<redacted-token>")
        return text

    def send_text(self, target: str, text: str, *, format: str = "plain") -> Delivery:
        if not self.token:
            raise ChannelError("TG_BOT_TOKEN is not configured")
        payload: dict[str, Any] = {
            "chat_id": target,
            "text": text,
            "disable_web_page_preview": True,
        }
        if format == "html":
            payload["parse_mode"] = "HTML"

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                r = requests.post(f"{self.api_base}/sendMessage", json=payload, timeout=self.timeout)
                data = r.json()
                if not data.get("ok"):
                    raise ChannelError(f"Telegram API failed: {data}")
                msg_id = str(data["result"]["message_id"])
                return Delivery(
                    channel=self.channel,
                    account_id=self.account_id,
                    conversation_id=str(target),
                    provider_message_id=msg_id,
                    editable=True,
                    raw=data,
                )
            except ChannelError:
                raise
            except (requests.RequestException, ValueError) as e:
                last_error = e
                if attempt == 3:
                    break
                time.sleep(attempt)
        raise ChannelError(f"Telegram send failed after 3 attempts: {self._safe_error_text(last_error)}")

    def edit_text(self, delivery: Delivery, text: str, *, format: str = "plain") -> bool:
        payload: dict[str, Any] = {
            "chat_id": delivery.conversation_id,
            "message_id": delivery.provider_message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if format == "html":
            payload["parse_mode"] = "HTML"
        r = requests.post(f"{self.api_base}/editMessageText", json=payload, timeout=self.timeout)
        if r.status_code == 400 and format == "html":
            try:
                data = r.json()
            except ValueError:
                data = {}
            description = str(data.get("description") or "")
            if "message is not modified" in description.lower():
                return True
            payload.pop("parse_mode", None)
            r = requests.post(f"{self.api_base}/editMessageText", json=payload, timeout=self.timeout)
        if r.status_code == 400:
            try:
                data = r.json()
            except ValueError:
                data = {}
            description = str(data.get("description") or "")
            if "message is not modified" in description.lower():
                return True
        if r.status_code == 429:
            try:
                retry_after = int((r.json().get("parameters") or {}).get("retry_after") or 1)
            except (TypeError, ValueError):
                retry_after = 1
            time.sleep(max(1, min(retry_after, 30)))
            r = requests.post(f"{self.api_base}/editMessageText", json=payload, timeout=self.timeout)
        if r.status_code == 200:
            return True
        r.raise_for_status()
        return True


class FeishuAdapter:
    channel = "feishu"
    capabilities = Capabilities(
        send_text=True,
        edit_text=False,
        markdown=False,
        html=False,
        card=False,
        streaming=False,
    )

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        default_conversation_id: str,
        receive_id_type: str = "chat_id",
        account_id: str | None = None,
        api_base: str = "https://open.feishu.cn/open-apis",
        timeout: int = 10,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.default_conversation_id = default_conversation_id
        self.receive_id_type = receive_id_type
        self.account_id = account_id or app_id or "default"
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._tenant_token: str | None = None
        self._tenant_token_expires_at = 0.0

    def default_target(self) -> str:
        if not self.default_conversation_id:
            raise ChannelError("FEISHU_HOME_CHANNEL is not configured")
        return self.default_conversation_id

    def _tenant_access_token(self) -> str:
        if self._tenant_token and time.time() < self._tenant_token_expires_at:
            return self._tenant_token
        if not self.app_id or not self.app_secret:
            raise ChannelError("FEISHU_APP_ID / FEISHU_APP_SECRET is not configured")
        r = requests.post(
            f"{self.api_base}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout,
        )
        data = r.json()
        if data.get("code") != 0:
            raise ChannelError(f"Feishu tenant_access_token failed: {data}")
        token = data.get("tenant_access_token")
        if not token:
            raise ChannelError(f"Feishu tenant_access_token missing: {data}")
        expire = int(data.get("expire") or 7200)
        self._tenant_token = token
        self._tenant_token_expires_at = time.time() + max(60, expire - 60)
        return token

    def send_text(self, target: str, text: str, *, format: str = "plain") -> Delivery:
        token = self._tenant_access_token()
        payload = {
            "receive_id": target,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        r = requests.post(
            f"{self.api_base}/im/v1/messages",
            params={"receive_id_type": self.receive_id_type},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=self.timeout,
        )
        data = r.json()
        if data.get("code") != 0:
            raise ChannelError(f"Feishu send_text failed: {data}")
        body = data.get("data") or {}
        msg_id = body.get("message_id") or (body.get("message") or {}).get("message_id")
        if not msg_id:
            raise ChannelError(f"Feishu send_text missing message_id: {data}")
        return Delivery(
            channel=self.channel,
            account_id=self.account_id,
            conversation_id=str(target),
            provider_message_id=str(msg_id),
            editable=False,
            raw=data,
        )

    def edit_text(self, delivery: Delivery, text: str, *, format: str = "plain") -> bool:
        return False

    def normalize_event(self, payload: dict[str, Any]) -> ChannelMessage | None:
        header = payload.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return None
        event = payload.get("event") or {}
        message = event.get("message") or {}
        chat_type = str(message.get("chat_type") or event.get("chat_type") or "").lower()
        if message.get("msg_type") != "text":
            return None
        content = _loads_json_object(message.get("content") or "")
        text = str(content.get("text") or "").strip()
        if not text:
            return None
        for mention in message.get("mentions") or []:
            key = mention.get("key")
            if key:
                text = text.replace(str(key), "").strip()
        sender_obj = event.get("sender", {}) or {}
        sender_id = sender_obj.get("sender_id", {})
        sender = sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id") or ""
        message_id = str(message.get("message_id") or "")
        if not message_id:
            return None
        return ChannelMessage(
            channel=self.channel,
            account_id=str(header.get("app_id") or self.account_id),
            conversation_id=str(message.get("chat_id") or ""),
            sender_id=str(sender),
            message_id=message_id,
            # Feishu explicitly recommends idempotency by message_id over event_id.
            event_id=message_id,
            thread_id=message.get("thread_id") or message.get("root_id"),
            text=text,
            raw={
                **payload,
                "chat_type": chat_type,
                "mentions": message.get("mentions") or [],
                "is_bot": bool(sender_obj.get("sender_type") == "bot" or sender_obj.get("is_bot")),
            },
        )


def _loads_json_object(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class MockAdapter:
    channel = "mock"
    account_id = "test"

    def __init__(self, *, edit_text: bool = True) -> None:
        self.capabilities = Capabilities(send_text=True, edit_text=edit_text, markdown=True, html=True)
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []

    def default_target(self) -> str:
        return "mock-conversation"

    def send_text(self, target: str, text: str, *, format: str = "plain") -> Delivery:
        msg_id = str(len(self.sent) + 1)
        self.sent.append({"target": target, "text": text, "format": format, "message_id": msg_id})
        return Delivery(
            channel=self.channel,
            account_id=self.account_id,
            conversation_id=target,
            provider_message_id=msg_id,
            editable=self.capabilities.edit_text,
            raw={"ok": True, "result": {"message_id": msg_id}},
        )

    def edit_text(self, delivery: Delivery, text: str, *, format: str = "plain") -> bool:
        self.edits.append({"delivery": delivery, "text": text, "format": format})
        return True


class ChannelGateway:
    def __init__(
        self,
        adapters: dict[str, ChannelAdapter],
        *,
        default_channel: str,
        db_path: Path = DB_FILE,
    ) -> None:
        self.adapters = adapters
        self.default_channel = default_channel
        self.db_path = db_path

    def register(self, adapter: ChannelAdapter) -> None:
        self.adapters[adapter.channel] = adapter

    def adapter_for(self, channel: str | None = None) -> ChannelAdapter:
        name = channel or self.default_channel
        try:
            return self.adapters[name]
        except KeyError:
            raise ChannelError(f"channel adapter is not configured: {name}")

    def send_text(
        self,
        text: str,
        *,
        source: str = "manual",
        channel: str | None = None,
        target: str | None = None,
        format: str = "plain",
    ) -> Delivery:
        adapter = self.adapter_for(channel)
        conversation_id = target or adapter.default_target()
        try:
            delivery = adapter.send_text(conversation_id, text, format=format)
            self._log_outbound(delivery, source=source, text=text, format=format, success=True, error=None)
            return delivery
        except Exception as e:
            failed = Delivery(
                channel=adapter.channel,
                account_id=adapter.account_id,
                conversation_id=str(conversation_id),
                provider_message_id="",
                editable=False,
                raw={},
            )
            self._log_outbound(failed, source=source, text=text, format=format, success=False, error=str(e))
            raise

    def edit_text(
        self,
        delivery: Delivery,
        text: str,
        *,
        source: str = "manual-edit",
        format: str = "plain",
    ) -> Delivery:
        adapter = self.adapter_for(delivery.channel)
        if delivery.editable and adapter.capabilities.edit_text:
            ok = adapter.edit_text(delivery, text, format=format)
            if ok:
                return delivery
        return self.send_text(
            text,
            source=source,
            channel=delivery.channel,
            target=delivery.conversation_id,
            format=format,
        )

    def log_inbound_start(self, message: ChannelMessage, *, db_path: Path | None = None) -> int | None:
        path = db_path or self.db_path
        if not path.exists():
            return None
        try:
            raw_json = json.dumps(message.raw, ensure_ascii=False) if message.raw else None
            with connect_close(path) as conn:
                cur = conn.execute(
                    """INSERT INTO channel_inbound_log
                       (timestamp, channel, account_id, conversation_id, thread_id,
                        sender_id, provider_msg_id, provider_event_id, dedupe_key,
                        raw_text, raw)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now().isoformat(timespec="seconds"),
                        message.channel,
                        message.account_id,
                        message.conversation_id,
                        message.thread_id,
                        message.sender_id,
                        message.message_id,
                        message.event_id,
                        message.dedupe_key(),
                        message.text,
                        raw_json,
                    ),
                )
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None
        except Exception as e:
            print(f"[channels] inbound log start failed: {e}", file=sys.stderr, flush=True)
            return None

    def log_inbound_update_parsed(
        self,
        inbound_id: int | None,
        *,
        parsed_command: str,
        parsed_intent: str | None = None,
        parsed_payload: dict[str, Any] | None = None,
        db_path: Path | None = None,
    ) -> None:
        if not inbound_id:
            return
        path = db_path or self.db_path
        payload_json = json.dumps(parsed_payload, ensure_ascii=False) if parsed_payload else None
        try:
            with connect_close(path) as conn:
                conn.execute(
                    """UPDATE channel_inbound_log
                       SET parsed_command=?, parsed_intent=?, parsed_payload=?
                       WHERE id=?""",
                    (parsed_command, parsed_intent, payload_json, inbound_id),
                )
        except Exception as e:
            print(f"[channels] inbound log parsed failed: {e}", file=sys.stderr, flush=True)

    def log_inbound_finish(
        self,
        inbound_id: int | None,
        *,
        response: Delivery | None,
        status: str,
        duration_ms: int,
        error: str | None = None,
        db_path: Path | None = None,
    ) -> None:
        if not inbound_id:
            return
        path = db_path or self.db_path
        try:
            with connect_close(path) as conn:
                conn.execute(
                    """UPDATE channel_inbound_log
                       SET response_channel=?, response_msg_id=?, handler_status=?,
                           duration_ms=?, handler_error=?
                       WHERE id=?""",
                    (
                        response.channel if response else None,
                        response.provider_message_id if response else None,
                        status,
                        duration_ms,
                        error,
                        inbound_id,
                    ),
                )
        except Exception as e:
            print(f"[channels] inbound log finish failed: {e}", file=sys.stderr, flush=True)

    def _log_outbound(
        self,
        delivery: Delivery,
        *,
        source: str,
        text: str,
        format: str,
        success: bool,
        error: str | None,
    ) -> None:
        if not self.db_path.exists():
            return
        try:
            raw_json = json.dumps(delivery.raw, ensure_ascii=False) if delivery.raw else None
            with connect_close(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO channel_outbound_log
                       (timestamp, channel, account_id, conversation_id, thread_id,
                        provider_msg_id, source, text, format, chunks, success, error, raw)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now().isoformat(timespec="seconds"),
                        delivery.channel,
                        delivery.account_id,
                        delivery.conversation_id,
                        delivery.thread_id,
                        delivery.provider_message_id or None,
                        source,
                        text,
                        format,
                        1,
                        1 if success else 0,
                        error[:500] if error else None,
                        raw_json,
                    ),
                )
        except Exception as e:
            print(f"[channels] outbound log failed: {e}", file=sys.stderr, flush=True)


_DEFAULT_GATEWAY: ChannelGateway | None = None


def load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_default_gateway() -> ChannelGateway:
    global _DEFAULT_GATEWAY
    if _DEFAULT_GATEWAY is not None:
        return _DEFAULT_GATEWAY
    load_env_file()
    default_channel = os.environ.get("CHANNEL_DEFAULT", "telegram").strip() or "telegram"
    adapters: dict[str, ChannelAdapter] = {}
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if token or chat_id or default_channel == "telegram":
        adapters["telegram"] = TelegramAdapter(token=token, default_conversation_id=chat_id)
    channels_enabled = {
        c.strip() for c in os.environ.get("CHANNELS_ENABLED", "").split(",") if c.strip()
    }
    feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_chat_id = os.environ.get("FEISHU_HOME_CHANNEL") or os.environ.get("FEISHU_DEFAULT_CHAT_ID", "")
    feishu_enabled = os.environ.get("FEISHU_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if (
        "feishu" in channels_enabled
        or feishu_enabled
        or default_channel == "feishu"
        or feishu_app_id
        or feishu_app_secret
        or feishu_chat_id
    ):
        adapters["feishu"] = FeishuAdapter(
            app_id=feishu_app_id,
            app_secret=feishu_app_secret,
            default_conversation_id=feishu_chat_id,
        )
    _DEFAULT_GATEWAY = ChannelGateway(adapters, default_channel=default_channel)
    return _DEFAULT_GATEWAY


def reset_default_gateway_for_tests() -> None:
    global _DEFAULT_GATEWAY
    _DEFAULT_GATEWAY = None

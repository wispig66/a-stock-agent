"""Unified IM gateway listener.

Telegram remains the stable production path. Feishu is handled by a small
gateway runtime that keeps SDK callbacks non-blocking and processes each chat
serially.
"""
from __future__ import annotations

import fcntl
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from stock_codex.apps import tg_listener
from stock_codex.channels import ChannelMessage, FeishuAdapter, get_default_gateway, load_env_file
from stock_codex.infra.logger import get_logger
from stock_codex.paths import DATA_DIR

log = get_logger("channel_listener")

GATEWAY_LOCK_FILE = DATA_DIR / "channel_gateway.lock"
GATEWAY_STATE_FILE = DATA_DIR / "channel_gateway_state.json"
FEISHU_DEDUP_FILE = DATA_DIR / "feishu_seen_message_ids.json"


def enabled_channels() -> set[str]:
    load_env_file()
    raw = os.environ.get("CHANNELS_ENABLED")
    if raw:
        return {x.strip() for x in raw.split(",") if x.strip()}
    if os.environ.get("FEISHU_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"feishu"}
    return {os.environ.get("CHANNEL_DEFAULT", "telegram").strip() or "telegram"}


@dataclass(frozen=True)
class GatewayTask:
    channel: str
    conversation_id: str
    message_id: str
    handle: Callable[[], None]


@dataclass(frozen=True)
class FeishuPolicy:
    allowed_chat_ids: frozenset[str]
    allowed_user_ids: frozenset[str]
    require_mention: bool = True
    allow_bots: bool = False
    bot_open_id: str | None = None

    @classmethod
    def from_env(cls) -> "FeishuPolicy":
        load_env_file()
        return cls(
            allowed_chat_ids=_split_env_set(
                os.environ.get("FEISHU_ALLOWED_CHAT_IDS")
                or os.environ.get("FEISHU_HOME_CHANNEL")
                or os.environ.get("FEISHU_DEFAULT_CHAT_ID")
                or ""
            ),
            allowed_user_ids=_split_env_set(os.environ.get("FEISHU_ALLOWED_USERS", "")),
            require_mention=_env_bool("FEISHU_REQUIRE_MENTION", default=True),
            allow_bots=os.environ.get("FEISHU_ALLOW_BOTS", "none").strip().lower() == "all",
            bot_open_id=os.environ.get("FEISHU_BOT_OPEN_ID") or None,
        )

    def allows(self, message: ChannelMessage) -> tuple[bool, str | None]:
        if message.is_from_bot and not self.allow_bots:
            return False, "bot message ignored"
        if self.allowed_chat_ids and "*" not in self.allowed_chat_ids:
            if message.conversation_id not in self.allowed_chat_ids:
                return False, "chat not allowed"
        if self.allowed_user_ids and "*" not in self.allowed_user_ids:
            if message.sender_id not in self.allowed_user_ids:
                return False, "sender not allowed"
        if self.require_mention and not message.is_direct_message:
            if not _message_mentions_bot(message, self.bot_open_id):
                return False, "group message without bot mention"
        return True, None


class PersistentDeduper:
    def __init__(self, path: Path, *, ttl_seconds: int = 24 * 60 * 60, max_entries: int = 2048) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._seen = self._load()

    def seen_or_mark(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._prune(now)
            if key in self._seen:
                return True
            self._seen[key] = now
            self._save()
            return False

    def _load(self) -> dict[str, float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._seen, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        self._seen = {k: ts for k, ts in self._seen.items() if ts >= cutoff}
        if len(self._seen) <= self.max_entries:
            return
        keep = sorted(self._seen.items(), key=lambda item: item[1])[-self.max_entries :]
        self._seen = dict(keep)


class GatewayRuntime:
    def __init__(
        self,
        *,
        policy: FeishuPolicy | None = None,
        deduper: PersistentDeduper | None = None,
        state_file: Path = GATEWAY_STATE_FILE,
    ) -> None:
        self.policy = policy or FeishuPolicy.from_env()
        self.deduper = deduper or PersistentDeduper(FEISHU_DEDUP_FILE)
        self.state_file = state_file
        self._queues: dict[str, queue.Queue[GatewayTask | None]] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running = False

    def start(self, *, channels: set[str]) -> None:
        self._running = True
        self.write_state(
            channels=sorted(channels),
            adapters={channel: "starting" for channel in channels},
            last_error=None,
        )

    def submit(self, message: ChannelMessage) -> bool:
        allowed, reason = self.policy.allows(message)
        if not allowed:
            log.info("Feishu inbound ignored: %s chat=%s sender=%s", reason, message.conversation_id, message.sender_id)
            self.write_state(last_inbound=self._now(), last_ignored=reason, adapters={"feishu": "running"})
            return False
        if self.deduper.seen_or_mark(message.dedupe_key()):
            log.info("Feishu inbound duplicate ignored: %s", message.dedupe_key())
            self.write_state(last_inbound=self._now(), last_ignored="duplicate", adapters={"feishu": "running"})
            return False
        return self.submit_task(
            GatewayTask(
                channel=message.channel,
                conversation_id=message.conversation_id,
                message_id=message.message_id,
                handle=lambda: tg_listener.handle_channel_message(message),
            )
        )

    def submit_task(self, task: GatewayTask) -> bool:
        chat_key = task.conversation_id or "default"
        with self._lock:
            q = self._queues.get(chat_key)
            if q is None:
                q = queue.Queue()
                self._queues[chat_key] = q
                worker = threading.Thread(target=self._chat_worker, args=(chat_key, q), name=f"im-{chat_key}", daemon=True)
                self._workers[chat_key] = worker
                worker.start()
            q.put(task)
        self.write_state(last_inbound=self._now(), adapters={task.channel: "running"})
        return True

    def _chat_worker(self, chat_key: str, q: queue.Queue[GatewayTask | None]) -> None:
        while True:
            task = q.get()
            if task is None:
                q.task_done()
                return
            try:
                task.handle()
                self.write_state(last_outbound=self._now(), adapters={task.channel: "running"}, last_error=None)
            except Exception as e:
                log.exception("%s message handler failed chat=%s msg=%s", task.channel, chat_key, task.message_id)
                self.write_state(adapters={task.channel: "error"}, last_error=_safe_error_text(e))
                if task.channel == "feishu":
                    try:
                        get_default_gateway().send_text(
                            "❌ 处理失败，请稍后重试",
                            source="feishu-listener-error",
                            channel="feishu",
                            target=task.conversation_id,
                            format="plain",
                        )
                    except Exception:
                        log.exception("Feishu failure notification failed")
            finally:
                q.task_done()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            queues = list(self._queues.values())
        for q in queues:
            q.put(None)
        self.write_state(adapters={"feishu": "stopped"})

    def write_state(self, **patch: Any) -> None:
        with self._state_lock:
            state = {}
            try:
                if self.state_file.exists():
                    loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        state.update(loaded)
            except Exception:
                pass
            adapters = dict(state.get("adapters") or {})
            adapters.update(patch.pop("adapters", {}) or {})
            state.update(patch)
            state["adapters"] = adapters
            state["pid"] = os.getpid()
            state["updated_at"] = self._now()
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_name(
                f"{self.state_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.state_file)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")


def feishu_message_from_sdk_event(data: Any, adapter: FeishuAdapter) -> ChannelMessage | None:
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    if event is None or message is None:
        return None
    msg_type = getattr(message, "msg_type", None) or getattr(message, "message_type", None)
    if msg_type != "text":
        return None
    sender = getattr(event, "sender", None)
    payload = {
        "header": {
            "event_type": "im.message.receive_v1",
            "app_id": adapter.app_id,
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": _nested_attr(sender, "sender_id", "open_id"),
                    "user_id": _nested_attr(sender, "sender_id", "user_id"),
                    "union_id": _nested_attr(sender, "sender_id", "union_id"),
                },
                "sender_type": getattr(sender, "sender_type", None),
                "is_bot": bool(getattr(sender, "is_bot", False)),
            },
            "message": {
                "message_id": getattr(message, "message_id", None),
                "chat_id": getattr(message, "chat_id", None),
                "chat_type": getattr(message, "chat_type", None),
                "msg_type": msg_type,
                "content": _json_content(getattr(message, "content", None)),
                "mentions": _mentions_to_dicts(getattr(message, "mentions", None)),
                "thread_id": getattr(message, "thread_id", None),
                "root_id": getattr(message, "root_id", None),
            },
        },
    }
    return adapter.normalize_event(payload)


def _nested_attr(obj: Any, *names: str) -> Any:
    cur = obj
    for name in names:
        cur = getattr(cur, name, None)
        if cur is None:
            return None
    return cur


def _mentions_to_dicts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    mentions = value if isinstance(value, list) else list(value)
    out: list[dict[str, Any]] = []
    for mention in mentions:
        if isinstance(mention, dict):
            out.append(mention)
        else:
            out.append({
                "key": getattr(mention, "key", None),
                "id": _id_to_dict(getattr(mention, "id", None)),
                "name": getattr(mention, "name", None),
            })
    return out


def _json_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return ""


def _id_to_dict(value: Any) -> Any:
    if value is None or isinstance(value, (str, dict)):
        return value
    return {
        "open_id": getattr(value, "open_id", None),
        "user_id": getattr(value, "user_id", None),
        "union_id": getattr(value, "union_id", None),
    }


def run_feishu_ws(runtime: GatewayRuntime | None = None) -> None:
    try:
        import lark_oapi as lark  # type: ignore
    except ImportError as e:
        raise RuntimeError("Feishu WebSocket listener requires `uv add lark-oapi`") from e

    runtime = runtime or GatewayRuntime()
    adapter = get_default_gateway().adapter_for("feishu")
    if not isinstance(adapter, FeishuAdapter):
        raise RuntimeError("configured feishu adapter is not FeishuAdapter")

    def on_message(data):
        msg = feishu_message_from_sdk_event(data, adapter)
        if msg is None:
            log.info("Feishu message event ignored: non-text or empty")
            return
        log.info("Feishu inbound queued chat=%s sender=%s text=%s",
                 msg.conversation_id, msg.sender_id, msg.text[:80])
        runtime.submit(msg)

    def on_p2p_entered(data):
        log.info("Feishu bot p2p entered event received")

    event_handler = (
        lark.EventDispatcherHandler.builder(
            os.environ.get("FEISHU_ENCRYPT_KEY", ""),
            os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
        )
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(on_p2p_entered)
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    ws_client = lark.ws.Client(
        app_id=adapter.app_id,
        app_secret=adapter.app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    runtime.write_state(adapters={"feishu": "running"}, last_error=None)
    log.info("Feishu WebSocket listener starting")
    ws_client.start()


def run_telegram_poll(runtime: GatewayRuntime | None = None) -> None:
    runtime = runtime or GatewayRuntime()
    _configure_telegram_from_env()
    if not tg_listener.TG_TOKEN or not tg_listener.ALLOWED_CHAT_ID:
        log.critical("TG_BOT_TOKEN / ALLOWED_CHAT_ID 未配置，退出")
        sys.exit(2)
    _poll_lock = tg_listener._acquire_poll_lock()
    offset = tg_listener._load_offset()
    log.info("Telegram poller starting offset=%d", offset)
    runtime.write_state(adapters={"telegram": "running"}, last_error=None)
    backoff = 1
    poll_failures = 0
    first_failure_at: float | None = None
    while True:
        try:
            updates = tg_listener._get_updates(offset)
            if poll_failures:
                downtime = time.monotonic() - (first_failure_at or time.monotonic())
                log.info("Telegram getUpdates recovered after %d failures, downtime %.1fs",
                         poll_failures, downtime)
                poll_failures = 0
                first_failure_at = None
            backoff = 1
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                tg_listener._save_offset(offset)
                msg = update.get("message") or update.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                reply_to = (msg.get("reply_to_message") or {}).get("message_id")
                user_msg_id = msg.get("message_id")
                if not text or chat_id is None:
                    continue
                runtime.submit_task(
                    GatewayTask(
                        channel="telegram",
                        conversation_id=str(chat_id),
                        message_id=str(update["update_id"]),
                        handle=lambda text=text, chat_id=chat_id, reply_to=reply_to,
                        update_id=update["update_id"], user_msg_id=user_msg_id: tg_listener.handle(
                            text,
                            chat_id,
                            reply_to_msg_id=reply_to,
                            update_id=update_id,
                            user_msg_id=user_msg_id,
                        ),
                    )
                )
        except Exception as e:
            poll_failures += 1
            if first_failure_at is None:
                first_failure_at = time.monotonic()
            should_alert = (
                poll_failures == tg_listener.POLL_ALERT_AFTER_FAILURES
                or (
                    poll_failures > tg_listener.POLL_ALERT_AFTER_FAILURES
                    and tg_listener.POLL_ALERT_EVERY_FAILURES > 0
                    and poll_failures % tg_listener.POLL_ALERT_EVERY_FAILURES == 0
                )
            )
            msg = ("Telegram getUpdates failed #%d (%s: %s), backoff %ds"
                   % (poll_failures, type(e).__name__, _safe_error_text(e)[:200], backoff))
            runtime.write_state(adapters={"telegram": "error"}, last_error=msg)
            if should_alert:
                log.error(msg, exc_info=True)
            else:
                log.warning(msg)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def main() -> None:
    channels = enabled_channels()
    _gateway_lock = _acquire_gateway_lock()
    runtime = GatewayRuntime()
    runtime.start(channels=channels)
    if "feishu" in channels and "telegram" in channels:
        t = threading.Thread(target=run_feishu_ws, kwargs={"runtime": runtime}, name="feishu-ws", daemon=True)
        t.start()
        run_telegram_poll(runtime=runtime)
        return
    if "feishu" in channels:
        run_feishu_ws(runtime=runtime)
        return
    run_telegram_poll(runtime=runtime)


def _acquire_gateway_lock():
    GATEWAY_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(GATEWAY_LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        log.critical("channel gateway already running; refuse duplicate listener")
        sys.exit(78)
    lock.write(str(os.getpid()))
    lock.truncate()
    lock.flush()
    return lock


def _split_env_set(raw: str) -> frozenset[str]:
    if raw.strip() == "*":
        return frozenset({"*"})
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _configure_telegram_from_env() -> None:
    load_env_file()
    tg_listener.TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
    tg_listener.TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
    tg_listener.ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID") or tg_listener.TG_CHAT_ID
    tg_listener.TG_API = f"https://api.telegram.org/bot{tg_listener.TG_TOKEN}"


def _message_mentions_bot(message: ChannelMessage, bot_open_id: str | None) -> bool:
    mentions = message.raw.get("mentions") or []
    if not mentions:
        return False
    if not bot_open_id:
        return True
    for mention in mentions:
        mention_id = mention.get("id") if isinstance(mention, dict) else None
        if isinstance(mention_id, dict) and mention_id.get("open_id") == bot_open_id:
            return True
        if isinstance(mention_id, str) and mention_id == bot_open_id:
            return True
    return False


def _safe_error_text(exc: Exception) -> str:
    text = str(exc)
    for key in ("TG_BOT_TOKEN", "FEISHU_APP_SECRET"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "<redacted>")
    return text[:500]


if __name__ == "__main__":
    main()

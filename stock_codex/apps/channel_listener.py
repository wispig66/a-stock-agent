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

from stock_codex.apps import command_router
from stock_codex.channels import ChannelMessage, FeishuAdapter, get_default_gateway, load_env_file
from stock_codex.channels.outbox import run_outbox_drain
from stock_codex.infra.logger import get_logger
from stock_codex.paths import DATA_DIR

log = get_logger("channel_listener")

GATEWAY_LOCK_FILE = DATA_DIR / "channel_gateway.lock"
GATEWAY_STATE_FILE = DATA_DIR / "channel_gateway_state.json"
FEISHU_DEDUP_FILE = DATA_DIR / "feishu_seen_message_ids.json"
FEISHU_MENU_TEXTS = {
    "help": command_router.HELP_TEXT,
    "menu_help": command_router.HELP_TEXT,
    "query": "直接发送 6 位股票代码或股票名称，例如：600519 或 贵州茅台。",
    "menu_query": "直接发送 6 位股票代码或股票名称，例如：600519 或 贵州茅台。",
    "ask": "发送 /ask <问题> 或 /ask+ <问题>，例如：/ask 光伏怎么样。",
    "menu_ask": "发送 /ask <问题> 或 /ask+ <问题>，例如：/ask 光伏怎么样。",
}


def enabled_channels() -> set[str]:
    load_env_file()
    raw = os.environ.get("CHANNELS_ENABLED")
    if raw:
        return {x.strip() for x in raw.split(",") if x.strip()}
    if os.environ.get("FEISHU_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"feishu"}
    return {os.environ.get("CHANNEL_DEFAULT", "feishu").strip() or "feishu"}


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
                handle=lambda: command_router.handle_channel_message(message),
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

    def on_bot_menu(data):
        handle_feishu_menu_event(data, adapter)

    event_handler = (
        lark.EventDispatcherHandler.builder(
            os.environ.get("FEISHU_ENCRYPT_KEY", ""),
            os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
        )
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(on_p2p_entered)
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_application_bot_menu_v6(on_bot_menu)
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


def handle_feishu_menu_event(data: Any, adapter: FeishuAdapter) -> bool:
    event = getattr(data, "event", None)
    event_key = str(getattr(event, "event_key", "") or "").strip()
    operator = getattr(event, "operator", None)
    operator_id = getattr(operator, "operator_id", None)
    open_id = getattr(operator_id, "open_id", None)
    if not event_key:
        log.info("Feishu bot menu event ignored: empty event_key")
        return False
    text = FEISHU_MENU_TEXTS.get(event_key)
    if not text:
        text = f"暂不支持的菜单：{event_key}\n\n{command_router.HELP_TEXT}"
    target = f"open_id:{open_id}" if open_id else adapter.default_target()
    log.info("Feishu bot menu clicked key=%s target=%s", event_key, "open_id" if open_id else "home")
    get_default_gateway().send_text(
        text,
        source=f"feishu-menu:{event_key}",
        channel="feishu",
        target=target,
        format="plain",
    )
    return True


def _dispatch_message(runtime: GatewayRuntime, message: ChannelMessage) -> bool:
    """Dedup + enqueue an inbound message to its per-chat worker.

    Channel-neutral: per-channel authorization is enforced downstream by
    command_router.handle (_is_allowed_chat). Feishu additionally pre-filters
    via FeishuPolicy in GatewayRuntime.submit; other channels route here.
    """
    if runtime.deduper.seen_or_mark(message.dedupe_key()):
        log.info("%s inbound duplicate ignored: %s", message.channel, message.dedupe_key())
        return False
    return runtime.submit_task(
        GatewayTask(
            channel=message.channel,
            conversation_id=message.conversation_id,
            message_id=message.message_id,
            handle=lambda: command_router.handle_channel_message(message),
        )
    )


def run_wecom_listener(runtime: GatewayRuntime | None = None) -> None:
    """WeCom 智能机器人长连接：收 aibot_msg_callback、发 aibot_send_msg。

    出站绑定在这条 WS 上，因此连接成功后注册 outbox sender，由 drain 线程消费。
    重连用指数退避；live 行为需用真实 WECOM_BOT_ID/SECRET 验收。
    """
    try:
        import websocket  # type: ignore  # websocket-client
    except ImportError as e:
        raise RuntimeError("WeCom listener requires `uv add websocket-client`") from e
    from stock_codex.channels.wecom import WeComAdapter, new_req_id
    from stock_codex.channels.outbox import register_outbox_sender, unregister_outbox_sender

    runtime = runtime or GatewayRuntime()
    adapter = get_default_gateway().adapter_for("wecom")
    if not isinstance(adapter, WeComAdapter):
        raise RuntimeError("configured wecom adapter is not WeComAdapter")

    state: dict[str, Any] = {"ws": None}
    send_lock = threading.Lock()

    def sender(target: str, text: str, fmt: str) -> str:
        ws = state["ws"]
        if ws is None:
            raise RuntimeError("wecom ws not connected")
        req_id = new_req_id("send")
        frame = adapter.send_frame(target, text, format=fmt, req_id=req_id)
        with send_lock:
            ws.send(json.dumps(frame, ensure_ascii=False))
        return req_id

    def on_open(ws):
        state["ws"] = ws
        with send_lock:
            ws.send(json.dumps(adapter.subscribe_frame(), ensure_ascii=False))
        register_outbox_sender("wecom", sender)
        runtime.write_state(adapters={"wecom": "running"}, last_error=None)
        log.info("WeCom listener subscribed bot=%s", adapter.bot_id)

    def on_message(ws, raw):
        try:
            frame = json.loads(raw)
        except Exception:
            return
        if frame.get("cmd") != "aibot_msg_callback":
            return
        msg = adapter.normalize_event(frame)
        if msg is None:
            return
        log.info("WeCom inbound chat=%s sender=%s text=%s",
                 msg.conversation_id, msg.sender_id, msg.text[:80])
        _dispatch_message(runtime, msg)

    def on_error(ws, err):
        exc = err if isinstance(err, Exception) else Exception(str(err))
        runtime.write_state(adapters={"wecom": "error"}, last_error=_safe_error_text(exc))
        log.warning("WeCom ws error: %s", err)

    def on_close(ws, *_args):
        state["ws"] = None
        unregister_outbox_sender("wecom")
        log.info("WeCom ws closed")

    def heartbeat():
        while True:
            time.sleep(30)
            ws = state["ws"]
            if ws is None:
                continue
            try:
                with send_lock:
                    ws.send(json.dumps({"cmd": "ping"}))
            except Exception:
                pass

    threading.Thread(target=heartbeat, name="wecom-ping", daemon=True).start()
    backoffs = [2, 5, 10, 30, 60]
    attempt = 0
    while True:
        try:
            app = websocket.WebSocketApp(
                adapter.ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            app.run_forever()
        except Exception:
            log.exception("WeCom ws run_forever crashed")
        attempt = min(attempt + 1, len(backoffs) - 1)
        time.sleep(backoffs[attempt])


def _load_json_dict(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def run_weixin_listener(runtime: GatewayRuntime | None = None) -> None:
    """个人微信 iLink 长轮询：getupdates 收消息、sendmessage 发消息（回带 context_token）。

    仅 1v1。出站经 outbox：sender 查 per-peer context_token 后调 sendmessage。
    需先用 scripts/configure_weixin.py 扫码登录写入 WEIXIN_TOKEN/WEIXIN_ACCOUNT_ID。
    live 行为需真实账号验收。
    """
    import requests
    from stock_codex.channels.weixin import WeixinAdapter
    from stock_codex.channels.outbox import register_outbox_sender, unregister_outbox_sender

    runtime = runtime or GatewayRuntime()
    adapter = get_default_gateway().adapter_for("weixin")
    if not isinstance(adapter, WeixinAdapter):
        raise RuntimeError("configured weixin adapter is not WeixinAdapter")
    if not adapter.token:
        raise RuntimeError("WEIXIN_TOKEN 未配置；先运行 scripts/configure_weixin.py 扫码登录")

    ctx_file = DATA_DIR / "weixin_context_tokens.json"
    buf_file = DATA_DIR / "weixin_updates_buf.txt"
    ctx_lock = threading.Lock()
    ctx_store = _load_json_dict(ctx_file)

    def _save_ctx() -> None:
        ctx_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = ctx_file.with_suffix(".tmp")
        with ctx_lock:
            tmp.write_text(json.dumps(ctx_store, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(ctx_file)

    def sender(target: str, text: str, fmt: str) -> str:
        token = ctx_store.get(target, "")
        body = adapter.send_payload(target, text, context_token=token)
        r = requests.post(
            f"{adapter.base_url}/ilink/bot/sendmessage",
            headers=adapter.auth_headers(), json=body, timeout=15,
        )
        r.raise_for_status()
        data = r.json() if r.content else {}
        if isinstance(data, dict) and data.get("ret") not in (0, None):
            raise RuntimeError(f"weixin sendmessage ret={data.get('ret')}")
        return str(data.get("svr_id") or "") if isinstance(data, dict) else ""

    register_outbox_sender("weixin", sender)
    runtime.write_state(adapters={"weixin": "running"}, last_error=None)
    log.info("WeChat iLink listener starting account=%s", adapter.account_id)

    buf = buf_file.read_text(encoding="utf-8").strip() if buf_file.exists() else ""
    backoff = 1
    try:
        while True:
            try:
                r = requests.post(
                    f"{adapter.base_url}/ilink/bot/getupdates",
                    headers=adapter.auth_headers(),
                    json=adapter.getupdates_payload(buf), timeout=40,
                )
                r.raise_for_status()
                data = r.json()
                backoff = 1
                for m in data.get("msgs") or []:
                    msg = adapter.normalize_event(m)
                    if msg is None:
                        continue
                    token = msg.raw.get("context_token")
                    if token:
                        ctx_store[msg.sender_id] = token
                        _save_ctx()
                    log.info("WeChat iLink inbound from=%s text=%s", msg.sender_id, msg.text[:80])
                    _dispatch_message(runtime, msg)
                new_buf = data.get("get_updates_buf")
                if new_buf is not None and new_buf != buf:
                    buf = str(new_buf)
                    buf_file.parent.mkdir(parents=True, exist_ok=True)
                    buf_file.write_text(buf, encoding="utf-8")
            except Exception as e:
                runtime.write_state(adapters={"weixin": "error"}, last_error=_safe_error_text(e))
                log.warning("WeChat iLink getupdates failed: %s", _safe_error_text(e)[:200])
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
    finally:
        unregister_outbox_sender("weixin")


def _listener_dispatch() -> dict[str, Callable[..., None]]:
    """Channel -> inbound listener. Built at call time so tests can monkeypatch
    the module-level listener functions. Outbound for connection-bound channels
    flows through the outbox regardless of whether an inbound listener exists."""
    return {"feishu": run_feishu_ws, "wecom": run_wecom_listener, "weixin": run_weixin_listener}


def main() -> None:
    channels = enabled_channels()
    _gateway_lock = _acquire_gateway_lock()
    runtime = GatewayRuntime()
    runtime.start(channels=channels)
    # Drain the outbox for connection-bound channels (wecom/weixin). Idle-cheap
    # when no such sender is registered; senders appear once their listener connects.
    drain = threading.Thread(
        target=run_outbox_drain,
        kwargs={
            "logger": get_default_gateway(),
            "should_stop": lambda: not getattr(runtime, "_running", False),
        },
        name="outbox-drain",
        daemon=True,
    )
    drain.start()
    dispatch = _listener_dispatch()
    listeners = [dispatch[ch] for ch in sorted(channels) if ch in dispatch]
    if not listeners:
        log.warning("no inbound listener for channels=%s; outbound still flows via outbox", channels)
        while getattr(runtime, "_running", False):
            time.sleep(3600)
        return
    # Run all but the last listener on background threads; the last blocks main.
    for fn in listeners[:-1]:
        threading.Thread(
            target=fn, kwargs={"runtime": runtime},
            name=f"listener-{fn.__name__}", daemon=True,
        ).start()
    listeners[-1](runtime=runtime)


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
    for key in ("FEISHU_APP_SECRET", "WECOM_SECRET", "WEIXIN_TOKEN"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "<redacted>")
    return text[:500]


if __name__ == "__main__":
    main()

"""IM 推送模块（含自动入库）。

推送统一经 ChannelGateway 发到当前启用的通道（feishu / wecom / …），
连接绑定型通道（wecom/weixin）会落到 outbox 由监听进程发送。

每次 push 都自动写一条到 SQLite `push_log` 表（兼容旧复盘逻辑）；跨通道主表是
`channel_outbound_log`，由 gateway 写。

用法：
    from stock_codex.infra.notify import push, push_md
    push("**Markdown**", source="stock-premarket")
"""

from __future__ import annotations
import os
import sys
from datetime import datetime
from stock_codex.channels import Delivery, get_default_gateway
from stock_codex.infra.db import connect as db_connect
from stock_codex.paths import DB_FILE as DB, ENV_FILE

# 读 .env
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# push_log.chat_id 审计字段：默认推送目标（仅用于旧复盘表）。
CHAT_ID = os.environ.get("FEISHU_HOME_CHANNEL") or os.environ.get("WECOM_HOME_CHANNEL") or ""


class NotifyError(RuntimeError):
    pass


def _safe_error_text(error: object) -> str:
    text = str(error)
    for key in ("FEISHU_APP_SECRET", "WECOM_SECRET", "WEIXIN_TOKEN"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _log_to_db(source: str, text: str, msg_id: int | None, chunks: int,
               success: bool, error: str | None) -> None:
    """写入 push_log 表。失败不影响推送主流程。"""
    if not DB.exists():
        return  # DB 还没建时静默跳过
    try:
        with db_connect(DB) as conn:
            conn.execute(
                """INSERT INTO push_log
                   (timestamp, source, chat_id, msg_id, text, chunks, success, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(timespec="seconds"),
                 source, CHAT_ID, msg_id, text, chunks,
                 1 if success else 0, error),
            )
    except Exception as e:
        # 不能在这里调 logger（会触发 ERROR→推送，再调 notify.push 死循环）
        print(f"[notify] 入库失败: {e}", file=sys.stderr, flush=True)


def push(text: str, source: str = "manual", raw: bool = False) -> dict:
    """推送到启用通道并自动入库。

    raw=True 按纯文本发送；否则按 markdown（适配器自行渲染卡片/纯文本）。
    """
    deliveries: list[Delivery] = []
    errors: list[str] = []
    for channel in _notify_channels():
        try:
            deliveries.append(_send_to_channel(text, source=source, raw=raw, channel=channel))
        except Exception as e:
            errors.append(f"{channel or 'default'}: {_safe_error_text(e)[:300]}")

    if not deliveries:
        error = "; ".join(errors)[:500] if errors else "no delivery"
        _log_to_db(source, text, None, 1, False, error)
        raise NotifyError(error)

    primary = deliveries[0]
    msg_id = _provider_msg_id_as_int(primary)
    partial_error = ("partial failure: " + "; ".join(errors))[:500] if errors else None
    _log_to_db(source, text, msg_id, 1, True, partial_error)
    if partial_error:
        print(f"[notify] {partial_error}", file=sys.stderr, flush=True)
    return _delivery_to_response(primary)


def _notify_channels() -> list[str | None]:
    raw = os.environ.get("CHANNELS_NOTIFY", "").strip()
    if not raw:
        return [None]
    channels = [x.strip() for x in raw.split(",") if x.strip()]
    return channels or [None]


def _send_to_channel(text: str, *, source: str, raw: bool, channel: str | None) -> Delivery:
    fmt = "plain" if raw else "markdown"
    return get_default_gateway().send_text(text, source=source, channel=channel, format=fmt)


def _provider_msg_id_as_int(delivery: Delivery) -> int | None:
    try:
        return int(delivery.provider_message_id)
    except (TypeError, ValueError):
        return None


def _delivery_to_response(delivery: Delivery) -> dict:
    if delivery.raw:
        return delivery.raw
    return {
        "ok": True,
        "result": {
            "message_id": delivery.provider_message_id,
            "chat": {"id": delivery.conversation_id},
        },
    }


def push_md(text: str, source: str = "manual") -> dict:
    """兼容旧接口，等价于 push()（按 markdown 渲染）。"""
    return push(text, source=source)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print(push("✅ 推送通道已连通", source="manual-test"))
    elif len(sys.argv) > 1 and sys.argv[1] == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        if not DB.exists():
            print("DB 不存在")
        else:
            with db_connect(DB) as c:
                rows = c.execute(
                    "SELECT id, timestamp, source, msg_id, length(text) as len, success "
                    "FROM push_log ORDER BY id DESC LIMIT ?", (n,)
                ).fetchall()
                print(f"最近 {n} 条推送：")
                for row in rows:
                    print(f"  #{row[0]} {row[1]} [{row[2]}] msg_id={row[3]} len={row[4]} ok={row[5]}")
    else:
        print("用法: uv run python -m stock_codex.infra.notify [test|tail [N]]")

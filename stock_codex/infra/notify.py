"""
Telegram Bot 推送模块（含自动入库）。

env vars（从 .env 读）：
    TG_BOT_TOKEN
    TG_CHAT_ID

每次 push 都自动写一条到 SQLite `push_log` 表，用于后续分析。

用法：
    from stock_codex.infra.notify import push, push_md
    push("文本", source="stock-premarket")
    push_md("**Markdown**", source="stock-postmarket")
"""

from __future__ import annotations
import html as html_mod
import os
import re
import sys
import time
from datetime import datetime
import requests
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

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
API = f"https://api.telegram.org/bot{TOKEN}"

class NotifyError(RuntimeError):
    pass


def _safe_error_text(error: object) -> str:
    text = str(error)
    if TOKEN:
        text = text.replace(TOKEN, "<redacted-token>")
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
        # 不能在这里调 logger（会触发 ERROR→TG，再调 notify.push 死循环）
        print(f"[notify] 入库失败: {e}", file=sys.stderr, flush=True)


# ============================================================
# Markdown → Telegram HTML 转换
# Telegram HTML 只支持 <b> <i> <u> <s> <code> <pre> <a>，不支持表格。
# 我们把 markdown 表格转成 bullet 列表 + 关键 markdown 语法转成对应 tag。
# ============================================================

_BOLD_PAT = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITAL_PAT = re.compile(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)")
_CODE_PAT = re.compile(r"`([^`\n]+?)`")
_HEAD_PAT = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_HR_PAT = re.compile(r"^---+\s*$", re.M)
# Telegram 原生支持的标签，遇到直接透传（不被 escape）
_TG_TAG_PAT = re.compile(
    r"<(b|strong|i|em|u|ins|s|strike|del|code|pre|tg-spoiler)>(.+?)</\1>",
    re.DOTALL | re.IGNORECASE)


def md_to_tg_html(text: str) -> str:
    """把混合 markdown 文本转成 Telegram HTML 可渲染格式。

    流程：所有要保留的 tag 全部塞进 stash 占位符 → HTML 转义剩余文本 →
    还原占位符（不再被 escape）。

    支持：**bold** *italic* `code` # 标题 ---分隔线 markdown 表格 - 列表
    """
    tokens: list[str] = []

    def _stash(html: str) -> str:
        idx = len(tokens)
        tokens.append(html)
        return f"\x00T{idx}\x00"

    # 1. markdown 表格 → bullet（先做，因为 | 不会被 escape 影响）
    text = _convert_md_tables(text, _stash)
    # 2. Telegram 原生标签透传（先做，避免被后续 markdown 规则当作 *...* 误伤）
    text = _TG_TAG_PAT.sub(
        lambda m: _stash(f"<{m.group(1).lower()}>{m.group(2)}</{m.group(1).lower()}>"),
        text)
    # 3. 标题 → <b>...</b>
    text = _HEAD_PAT.sub(lambda m: _stash(f"<b>{m.group(1).strip()}</b>"), text)
    # 4. 水平线删除
    text = _HR_PAT.sub("", text)
    # 5. 行内 markdown → tags
    text = _BOLD_PAT.sub(lambda m: _stash(f"<b>{m.group(1)}</b>"), text)
    text = _CODE_PAT.sub(lambda m: _stash(f"<code>{m.group(1)}</code>"), text)
    text = _ITAL_PAT.sub(lambda m: _stash(f"<i>{m.group(1)}</i>"), text)
    # 5. HTML 转义剩余原文（占位符 \x00T0\x00 不含 HTML 特殊字符，不受影响）
    text = html_mod.escape(text, quote=False)
    # 6. 还原占位符
    for i, v in enumerate(tokens):
        text = text.replace(f"\x00T{i}\x00", v)
    # 7. 折叠 3+ 连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _convert_md_tables(text: str, stash) -> str:
    """检测 markdown 表格块（含表头分隔行 |---|---|）改成 bullet 段。
    每条 bullet 含 <b>...</b>，所以用 stash 占位避免后续被 escape。
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if (stripped.startswith("|") and stripped.endswith("|") and
                i + 1 < len(lines) and
                re.match(r"^\s*\|[\s:|\-]+\|\s*$", lines[i + 1])):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2  # skip header + separator
            while i < len(lines) and lines[i].strip().startswith("|") \
                    and lines[i].strip().endswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if cells:
                    head_val = cells[0]
                    rest = []
                    for h, c in zip(header[1:], cells[1:]):
                        if c and c != "-":
                            rest.append(f"{h} {c}")
                    bullet = (f"<b>{head_val}</b>" +
                              (f" — {' · '.join(rest)}" if rest else ""))
                    out.append("• " + stash(bullet))
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _send(text: str, parse_mode: str | None = None) -> dict:
    if not TOKEN or not CHAT_ID:
        raise NotifyError(f"TG_BOT_TOKEN / TG_CHAT_ID 未配置，检查 {ENV_FILE}")
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = requests.post(f"{API}/sendMessage", json=payload, timeout=10)
            data = r.json()
            if not data.get("ok"):
                raise NotifyError(f"Telegram API 失败: {data}")
            return data
        except NotifyError:
            raise
        except (requests.RequestException, ValueError) as e:
            last_error = e
            if attempt == 3:
                break
            time.sleep(attempt)
    raise NotifyError(f"Telegram send failed after 3 attempts: {_safe_error_text(last_error)}")


def push(text: str, source: str = "manual", raw: bool = False) -> dict:
    """默认：自动 md→HTML 渲染 + 自动入库。
    raw=True 跳过转换，按纯文本发送（emoji / 纯文本 / 调试用）。
    """
    deliveries: list[Delivery] = []
    errors: list[str] = []
    warnings: list[str] = []
    for channel in _notify_channels():
        try:
            delivery, warning = _send_to_channel(text, source=source, raw=raw, channel=channel)
            deliveries.append(delivery)
            if warning:
                warnings.append(warning)
        except Exception as e:
            errors.append(f"{channel or 'default'}: {_safe_error_text(e)[:300]}")

    if not deliveries:
        error = "; ".join(errors)[:500] if errors else "no delivery"
        _log_to_db(source, text, None, 1, False, error)
        raise NotifyError(error)

    primary = deliveries[0]
    msg_id = _provider_msg_id_as_int(primary)
    notes = warnings + ([f"partial failure: {'; '.join(errors)}"] if errors else [])
    partial_error = "; ".join(notes)[:500] if notes else None
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


def _send_to_channel(text: str, *, source: str, raw: bool, channel: str | None) -> tuple[Delivery, str | None]:
    if raw:
        return get_default_gateway().send_text(text, source=source, channel=channel, format="plain"), None
    if channel in (None, "", "telegram"):
        body = md_to_tg_html(text)
        try:
            return get_default_gateway().send_text(body, source=source, channel=channel, format="html"), None
        except Exception as e:
            if "can't parse" not in str(e).lower():
                raise
            return (
                get_default_gateway().send_text(text, source=source, channel=channel, format="plain"),
                "HTML parse failed, fallback to plaintext",
            )
    return get_default_gateway().send_text(text, source=source, channel=channel, format="markdown"), None


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
    """兼容旧接口，等价于 push()（已自动 md→HTML）。"""
    return push(text, source=source)


def get_chat_id_helper() -> None:
    if not TOKEN:
        print("先设 TG_BOT_TOKEN")
        return
    r = requests.get(f"{API}/getUpdates", timeout=10).json()
    if not r.get("ok"):
        print("API 失败:", r)
        return
    seen = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat", {})
        cid = chat.get("id")
        if cid is not None:
            seen[cid] = chat.get("first_name") or chat.get("title") or chat.get("username") or "?"
    if not seen:
        print("没拉到对话。先在 Telegram 里给 bot 发一条消息再跑这个。")
    else:
        print("发现以下 chat：")
        for cid, name in seen.items():
            print(f"  chat_id = {cid}  ({name})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "whoami":
        get_chat_id_helper()
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        print(push("✅ Telegram bot 已连通", source="manual-test"))
    elif len(sys.argv) > 1 and sys.argv[1] == "tail":
        # 查最近 push 日志
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
        print("用法: uv run stock_codex.infra.notify [whoami|test|tail [N]]")

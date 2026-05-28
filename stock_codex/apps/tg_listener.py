"""TG 长轮询守护进程：接收单股代码/名称 → 调 Codex headless 跑 stock-query → 回卡片。

并发：fcntl 文件锁 + 排队计数器；1 跑 + 3 等 = 4 容量，第 5 拒绝。
失败重试：TG API 指数退避，Codex 子进程超时 180s 直接报错。
进程崩溃由 launchd KeepAlive 拉起；offset 持久化到 data/tg_offset.txt。
"""
from __future__ import annotations
import contextvars
import fcntl
import html as html_mod
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import re
import requests
import yaml

from stock_codex.channels import ChannelMessage, Delivery, get_default_gateway
from stock_codex.market import query  # noqa: E402
from stock_codex.domain import holdings as holdings_lib  # noqa: E402
from stock_codex.market.card_validator import (  # noqa: E402
    validate_card, load_stock_name_dict, format_violations,
)
from stock_codex.infra.notify import md_to_tg_html  # noqa: E402
from stock_codex.infra.db import connect  # noqa: E402
from stock_codex.infra.logger import get_logger, new_req_id, set_req_id, get_req_id  # noqa: E402
from stock_codex.paths import DATA_DIR, DB_FILE, HOLDINGS_FILE, PROJECT_ROOT

log = get_logger("tg_listener")

def _find_codex_bin() -> str:
    for p in (Path.home() / ".nvm/versions/node/v24.15.0/bin/codex",
              Path.home() / ".local/bin/codex",
              Path("/opt/homebrew/bin/codex"),
              Path("/usr/local/bin/codex")):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return "codex"

CODEX_BIN = _find_codex_bin()

_ASK_RE = re.compile(r"^/ask(\+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)

ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

ROOT = PROJECT_ROOT
OFFSET_FILE = DATA_DIR / "tg_offset.txt"
LOCK_FILE = "/tmp/stock-query.lock"
POLL_LOCK_FILE = DATA_DIR / "tg_listener.lock"
MAX_QUEUE = 3
_SKILL_TIMEOUT_NORMAL = 180
_SKILL_TIMEOUT_DEEP = 300
SKILL_TIMEOUT = _SKILL_TIMEOUT_NORMAL  # default; per-call override via run_skill_streaming_generic
EDIT_THROTTLE = 1.0         # Telegram editMessageText 限速：≥1s/chat 才安全
TG_MAX_LEN = 4000           # 4096 上限，留 96 字 buffer
POLL_ALERT_AFTER_FAILURES = int(os.environ.get("TG_POLL_ALERT_AFTER_FAILURES", "3"))
POLL_ALERT_EVERY_FAILURES = int(os.environ.get("TG_POLL_ALERT_EVERY_FAILURES", "20"))

_running = 0
_waiting = 0
_channel_inbound_ids: dict[int, int] = {}
_CURRENT_CHANNEL: contextvars.ContextVar[str] = contextvars.ContextVar("channel", default="telegram")
_CURRENT_TARGET: contextvars.ContextVar[str | None] = contextvars.ContextVar("target", default=None)
_CURRENT_ACCOUNT: contextvars.ContextVar[str] = contextvars.ContextVar("account", default="default")
_response_deliveries: dict[str, Delivery] = {}


def _safe_error_text(exc: Exception) -> str:
    text = str(exc)
    if TG_TOKEN:
        text = text.replace(TG_TOKEN, "<redacted-token>")
    return text

DB_PATH = DB_FILE

CARD_VALIDATOR_MODE = os.environ.get("CARD_VALIDATOR_MODE", "warn").lower()


def _validate_card_for_push(card_text: str, source: str) -> tuple[bool, list, Optional[Path]]:
    """读 data/allowed_latest_<source>.json 校验 card_text。

    返回 (ok, violations, log_file)。无 allowed 文件 = 跳过 (ok=True, [])。
    违规会落审计 data/card_violations/<ts>_<source>.json。
    """
    if CARD_VALIDATOR_MODE == "off":
        return (True, [], None)
    allowed_file = ROOT / "data" / f"allowed_latest_{source}.json"
    if not allowed_file.exists():
        return (True, [], None)
    try:
        allowed = json.loads(allowed_file.read_text(encoding="utf-8"))
        name_dict = load_stock_name_dict(DB_PATH) if DB_PATH.exists() else None
        ok, violations = validate_card(card_text, allowed, stock_name_dict=name_dict)
        if violations:
            log_dir = ROOT / "data" / "card_violations"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{int(time.time())}_{source}.json"
            log_file.write_text(json.dumps({
                "ts": datetime.now().isoformat(),
                "source": source,
                "mode": CARD_VALIDATOR_MODE,
                "req_id": get_req_id(),
                "card_text": card_text,
                "violations": [v.to_dict() for v in violations],
            }, ensure_ascii=False, indent=2))
            log.warning("card_validator [%s] %s 处违规 -> %s",
                        CARD_VALIDATOR_MODE, len(violations), log_file.name)
            return (ok, violations, log_file)
        return (True, [], None)
    except Exception as e:
        log.exception("card_validator 异常（fail-open）：%s", e)
        return (True, [], None)


def parse_ask_command(text: str) -> Optional[dict]:
    """解析 /ask <text> 或 /ask+ <text>。空 payload 返回 None。"""
    m = _ASK_RE.match(text.strip())
    if not m:
        return None
    is_deep = bool(m.group(1))
    payload = (m.group(2) or "").strip()
    if not payload:
        return None
    return {"mode": "deep" if is_deep else "normal", "payload": payload}


def skill_timeout_for(mode: str) -> int:
    return _SKILL_TIMEOUT_DEEP if mode == "deep" else _SKILL_TIMEOUT_NORMAL


def log_inbound_start(*, update_id: int, chat_id, user_msg_id: int, raw_text: str) -> Optional[int]:
    """写入 tg_inbound 一行，返回 inbound_id。update_id 重复 → None（去重）。"""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with connect(DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO tg_inbound(timestamp,update_id,chat_id,user_msg_id,raw_text) "
                "VALUES(?,?,?,?,?)",
                (ts, update_id, str(chat_id), user_msg_id, raw_text),
            )
            conn.commit()
            inbound_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    channel_id = get_default_gateway().log_inbound_start(
        ChannelMessage(
            channel="telegram",
            account_id="default",
            conversation_id=str(chat_id),
            sender_id=str(chat_id),
            message_id=str(user_msg_id),
            event_id=str(update_id),
            text=raw_text,
            raw={"update_id": update_id, "chat_id": chat_id, "user_msg_id": user_msg_id},
        ),
        db_path=DB_PATH,
    )
    if channel_id:
        _channel_inbound_ids[inbound_id] = channel_id
    return inbound_id


def log_inbound_update_parsed(inbound_id: int, *, parsed_command: str,
                              parsed_intent: Optional[str] = None,
                              parsed_payload: Optional[dict] = None) -> None:
    payload_json = json.dumps(parsed_payload, ensure_ascii=False) if parsed_payload else None
    with connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE tg_inbound SET parsed_command=?, parsed_intent=?, parsed_payload=? WHERE id=?",
            (parsed_command, parsed_intent, payload_json, inbound_id),
        )
        conn.commit()
    get_default_gateway().log_inbound_update_parsed(
        _channel_inbound_ids.get(inbound_id),
        parsed_command=parsed_command,
        parsed_intent=parsed_intent,
        parsed_payload=parsed_payload,
        db_path=DB_PATH,
    )


def log_inbound_finish(inbound_id: int, *, response_msg_id: Optional[object],
                       status: str, duration_ms: int, error: Optional[str] = None) -> None:
    legacy_msg_id = _int_or_none(response_msg_id)
    with connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE tg_inbound SET response_msg_id=?, handler_status=?, "
            "duration_ms=?, handler_error=? WHERE id=?",
            (legacy_msg_id, status, duration_ms, error, inbound_id),
        )
        conn.commit()
    response = _delivery_for_response(response_msg_id)
    get_default_gateway().log_inbound_finish(
        _channel_inbound_ids.pop(inbound_id, None),
        response=response,
        status=status,
        duration_ms=duration_ms,
        error=error,
        db_path=DB_PATH,
    )


def _int_or_none(value: object | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _delivery_for_response(response_msg_id: object | None) -> Delivery | None:
    if response_msg_id is None:
        return None
    key = str(response_msg_id)
    if key in _response_deliveries:
        return _response_deliveries[key]
    return Delivery(
        channel=_CURRENT_CHANNEL.get(),
        account_id=_CURRENT_ACCOUNT.get(),
        conversation_id=str(_CURRENT_TARGET.get() or TG_CHAT_ID),
        provider_message_id=key,
        editable=True,
    )


# ============================================================
# Telegram 低层 API（发送 / 编辑）
# ============================================================

def _tg_send(text: str, parse_mode: Optional[str] = None) -> int | str:
    """发新消息，返回 message_id。"""
    channel = _CURRENT_CHANNEL.get()
    target = str(_CURRENT_TARGET.get() or TG_CHAT_ID)
    body, fmt = _format_for_channel(text[:TG_MAX_LEN], parse_mode=parse_mode, channel=channel)
    delivery = get_default_gateway().send_text(
        body,
        source=f"{channel}-listener",
        channel=channel,
        target=target,
        format=fmt,
    )
    _response_deliveries[delivery.provider_message_id] = delivery
    return int(delivery.provider_message_id) if delivery.provider_message_id.isdigit() else delivery.provider_message_id


def _tg_edit(message_id: int | str, text: str, parse_mode: Optional[str] = None) -> None:
    """编辑已发消息。HTML parse 失败时回退纯文本（流式过程中 markdown 半截不可避免）。"""
    delivery = _delivery_for_response(message_id)
    if delivery is None:
        return
    if delivery.channel != "telegram" and not delivery.editable:
        # Feishu v1 has no edit/streaming path. Keep the first ack message,
        # drop transient progress updates, and only send final/error text.
        if parse_mode is None and not text.startswith(("❌", "⚠️", "⌛")):
            return
    new_delivery = get_default_gateway().edit_text(
        delivery,
        _format_for_channel(text[:TG_MAX_LEN], parse_mode=parse_mode, channel=delivery.channel)[0],
        source="tg-listener-edit",
        format=_format_for_channel(text[:TG_MAX_LEN], parse_mode=parse_mode, channel=delivery.channel)[1],
    )
    _response_deliveries[str(message_id)] = new_delivery
    _response_deliveries[new_delivery.provider_message_id] = new_delivery


def push_reply(text: str) -> None:
    """非流式回 TG（拒绝卡 / 错误提示 用）。"""
    try:
        if _CURRENT_CHANNEL.get() == "telegram":
            _tg_send(md_to_tg_html(text), parse_mode="HTML")
        else:
            _tg_send(text)
    except Exception:
        log.exception("push_reply 失败")


def _format_for_channel(text: str, *, parse_mode: Optional[str], channel: str) -> tuple[str, str]:
    if parse_mode == "HTML" and channel == "telegram":
        return text, "html"
    if parse_mode == "HTML":
        return _tg_html_to_plain_text(text), "plain"
    return text, "plain"


def _tg_html_to_plain_text(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|pre|blockquote)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html_mod.unescape(text).strip()


# ============================================================
# 业务
# ============================================================

def held_codes() -> set[str]:
    if not HOLDINGS_FILE.exists():
        return set()
    try:
        data = yaml.safe_load(HOLDINGS_FILE.read_text()) or {}
    except Exception:
        log.exception("holdings.yaml 解析失败")
        return set()
    return {str(h.get("code")).zfill(6) for h in (data.get("holdings") or [])
            if h.get("code")}


def run_skill_streaming(code: str, mode: str,
                        on_text: Callable[[str], None],
                        on_tool: Callable[[str], None]) -> str:
    """跑 stock-query skill。

    on_text(accumulated)：最终输出回调，调用方自行节流。
    on_tool(tool_name)：开始时回调，用于 loading 状态。
    返回最终完整卡片文本。
    """
    prompt = (f"请使用 stock-query skill 分析这只股票，严格按 SKILL.md "
              f"模板输出卡片，不要任何额外文字：code={code} mode={mode}")
    return _run_codex_exec(prompt=prompt, timeout=SKILL_TIMEOUT,
                           label=f"query:{code}:{mode}",
                           on_text=on_text, on_tool=on_tool)


def run_skill_streaming_generic(*, prompt: str, timeout: int,
                                on_text: Callable[[str], None],
                                on_tool: Callable[[str], None]) -> str:
    """通用 Codex headless 调用。返回最终卡片文本。
    与 run_skill_streaming 的差异：prompt + timeout 都是入参，不依赖模块级 SKILL_TIMEOUT。"""
    return _run_codex_exec(prompt=prompt, timeout=timeout, label="generic",
                           on_text=on_text, on_tool=on_tool)


def _run_codex_exec(*, prompt: str, timeout: int, label: str,
                    on_text: Callable[[str], None],
                    on_tool: Callable[[str], None]) -> str:
    on_tool("codex")
    env = os.environ.copy()
    env["STOCK_REQ_ID"] = get_req_id()
    start = time.time()
    with tempfile.NamedTemporaryFile("r+", encoding="utf-8", delete=True) as out:
        cmd = [
            CODEX_BIN,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(ROOT),
            "--output-last-message",
            out.name,
            "-",
        ]
        log.info("codex_exec(%s) 启动 timeout=%ss prompt_head=%s",
                 label, timeout, prompt[:80].replace("\n", " "))
        try:
            result = subprocess.run(
                cmd,
                cwd=str(ROOT),
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.error("codex_exec(%s) 超时 %ds", label, timeout)
            raise
        out.seek(0)
        final_text = out.read().strip()
    if result.returncode != 0:
        err = (result.stderr or "")[-4000:]
        log.error("codex_exec(%s) 退出码 %d\nstderr:\n%s", label, result.returncode, err)
        raise RuntimeError(f"codex exec 退出码 {result.returncode}: {(result.stderr or '')[:500]}")
    if not final_text:
        final_text = (result.stdout or "").strip()
    on_text(final_text)
    log.info("codex_exec(%s) 完成 用时 %.1fs len=%d", label, time.time() - start, len(final_text))
    return final_text


def _reject(code: str, reason: str) -> str:
    return f"❌ {code}\n原因：{reason}"


# ============================================================
# 交易流水：/buy /sell 命令解析 + 落库
# ============================================================

BUY_REASONS = ("二板接力", "龙头补涨", "火箭跟", "自主")
SELL_REASONS = ("止盈", "破位", "跳水", "换股")
TRADES_DB = DB_FILE
BUY_REASON_GENRE = {
    "二板接力": "A",
    "龙头补涨": "B",
    "火箭跟": "D",
    "自主": "未标记",
}

HELP_TEXT = (
    "📒 交易流水命令\n"
    "\n"
    "格式：\n"
    "  /buy  <代码> <价格> <手数> [理由] [@HH:MM]\n"
    "  /sell <代码> <价格> <手数> [理由] [@HH:MM]\n"
    "\n"
    "例子：\n"
    "  /buy 600519 12.34 10 二板接力\n"
    "  /sell 600519 15.0 5 止盈 @09:35\n"
    "\n"
    "📌 关联推送：长按某条系统卡片 → 回复 /buy …，"
    "自动记录是哪条推送触发的。\n"
    "\n"
    "买入理由（4 选 1，可省）：\n"
    f"  {' / '.join(BUY_REASONS)}\n"
    "卖出理由（4 选 1，可省）：\n"
    f"  {' / '.join(SELL_REASONS)}\n"
    "\n"
    "📐 数量用手数（1 手 = 100 股）。\n"
    "⏰ @HH:MM 指定当日成交时间，省略则用 TG 收到时间。\n"
    "\n"
    "其它：直接发 6 位代码或股票名 → 单股分析。"
)


def _trade_usage(side: str) -> str:
    """空 /buy 或 /sell 时返回该 side 的简要说明。"""
    reasons = BUY_REASONS if side == "buy" else SELL_REASONS
    side_cn = "买入" if side == "buy" else "卖出"
    example = (
        "/buy 600519 12.34 10 二板接力"
        if side == "buy"
        else "/sell 600519 15.0 5 止盈 @09:35"
    )
    return (
        f"📒 {side_cn}流水\n"
        f"格式：/{side} <代码> <价格> <手数> [理由] [@HH:MM]\n"
        f"例子：{example}\n"
        f"理由：{' / '.join(reasons)}\n"
        f"（手数 = 股数 ÷ 100；详细见 /help）"
    )


def parse_trade_command(text: str) -> Optional[dict]:
    """解析 /buy /sell 命令。

    格式：/buy <代码> <价格> <手数> [理由] [@HH:MM]
    返回 dict {side, code, price, qty, reason, ts_override}，
    或 None（非 trade 命令）。校验失败抛 ValueError。
    """
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lower()
    if cmd not in ("/buy", "/sell"):
        return None
    side = cmd[1:]

    tokens = list(parts[1:])
    ts_override: Optional[str] = None
    for i, tok in enumerate(tokens):
        if tok.startswith("@"):
            hhmm = tok[1:]
            try:
                hh, mm = hhmm.split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError
            except Exception:
                raise ValueError(f"时间格式错（用 @HH:MM）：{tok}")
            ts_override = hhmm
            tokens = tokens[:i] + tokens[i + 1:]
            break

    if len(tokens) < 3:
        raise ValueError(_trade_usage(side))

    code, price_s, qty_s = tokens[0], tokens[1], tokens[2]
    reason = " ".join(tokens[3:]).strip() if len(tokens) > 3 else None

    if not (len(code) == 6 and code.isdigit()):
        raise ValueError(f"代码必须 6 位数字，当前：{code}\n例：/{side} 600519 12.34 10")
    try:
        price = float(price_s)
    except ValueError:
        raise ValueError(f"价格不是数字：{price_s}\n例：/{side} {code} 12.34 10")
    if price <= 0:
        raise ValueError(f"价格必须 > 0：{price}")
    try:
        lots = int(qty_s)
    except ValueError:
        raise ValueError(
            f"手数必须是正整数（1 手 = 100 股）：{qty_s}\n"
            f"例：/{side} {code} {price_s} 10"
        )
    if lots <= 0:
        raise ValueError(f"手数必须 > 0：{lots}")

    valid = BUY_REASONS if side == "buy" else SELL_REASONS
    if reason and reason not in valid:
        side_cn = "买入" if side == "buy" else "卖出"
        raise ValueError(
            f"{side_cn}理由不识别：{reason}\n"
            f"可选 4 个：{' / '.join(valid)}\n"
            f"（理由可省，省略也行）"
        )

    return {
        "side": side,
        "code": code,
        "price": price,
        "qty": lots * 100,
        "reason": reason,
        "ts_override": ts_override,
    }


def _build_ts(ts_override: Optional[str], now: Optional[datetime] = None) -> str:
    """根据 @HH:MM 覆盖当前时间，否则用 now。返回 ISO 8601 秒精度。"""
    n = now or datetime.now()
    if ts_override:
        hh, mm = ts_override.split(":")
        n = n.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    return n.isoformat(timespec="seconds")


def record_trade(parsed: dict, source_msg_id: Optional[int],
                 db_path: Optional[Path] = None,
                 now: Optional[datetime] = None) -> int:
    """写一条 trades，返回 row id。"""
    ts = _build_ts(parsed["ts_override"], now=now)
    path = db_path or TRADES_DB
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(
            "INSERT INTO trades(ts, code, side, price, qty, reason, "
            "source_msg_id, note) VALUES(?,?,?,?,?,?,?,?)",
            (ts, parsed["code"], parsed["side"], parsed["price"],
             parsed["qty"], parsed["reason"], source_msg_id, None),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _stock_name_for_code(code: str) -> str:
    try:
        conn = sqlite3.connect(TRADES_DB)
        try:
            row = conn.execute("SELECT name FROM stock_basic WHERE code=?", (code,)).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return str(row[0])
    except Exception:
        log.exception("stock_basic 查询失败 code=%s", code)
    return code


def _sync_trade_to_holdings(parsed: dict, now: Optional[datetime] = None) -> str:
    ts = _build_ts(parsed["ts_override"], now=now)
    trade_date = datetime.fromisoformat(ts).date()
    if parsed["side"] == "buy":
        rec = holdings_lib.Holding(
            code=parsed["code"],
            name=_stock_name_for_code(parsed["code"]),
            genre=BUY_REASON_GENRE.get(parsed["reason"] or "", "未标记"),
            cost=parsed["price"],
            shares=parsed["qty"],
            buy_date=trade_date,
            source="bot_buy",
            note=parsed["reason"] or "TG /buy",
        )
        final = holdings_lib.upsert_holding(rec)
        return (
            f"已同步 holdings.yaml：{final.name} {final.shares} 股，"
            f"成本 {final.cost}，T+1 解锁 {final.unlock_date.isoformat()}"
        )

    try:
        old, remaining = holdings_lib.reduce_holding(parsed["code"], parsed["qty"])
    except KeyError:
        return "⚠️ trades 已记录；holdings.yaml 未找到该持仓，未能同步扣减"
    if remaining is None:
        return f"已同步 holdings.yaml：{old.name} 已清仓"
    return f"已同步 holdings.yaml：{remaining.name} 剩余 {remaining.shares} 股"


def handle_trade(parsed: dict, source_msg_id: Optional[int]) -> str:
    """落库 + 返回给用户看的确认文本。"""
    row_id = record_trade(parsed, source_msg_id)
    side_cn = "买入" if parsed["side"] == "buy" else "卖出"
    lots = parsed["qty"] // 100
    parts = [
        f"✅ #{row_id} {side_cn} {parsed['code']}",
        f"价 {parsed['price']} × {lots} 手（{parsed['qty']} 股）",
    ]
    if parsed["reason"]:
        parts.append(f"理由：{parsed['reason']}")
    if parsed["ts_override"]:
        parts.append(f"成交时间：{parsed['ts_override']}")
    if source_msg_id:
        parts.append(f"关联推送 msg_id={source_msg_id}")
    try:
        parts.append(_sync_trade_to_holdings(parsed))
    except Exception as e:
        log.exception("holdings.yaml 同步失败 trade_id=%s", row_id)
        parts.append(f"⚠️ trades 已记录；holdings.yaml 同步失败：{e}")
    return "\n".join(parts)


def handle(text: str, chat_id, today: Optional[str] = None,
           reply_to_msg_id: Optional[int] = None,
           update_id: Optional[int] = None,
           user_msg_id: Optional[int] = None,
           channel: str = "telegram",
           account_id: str = "default",
           channel_message: Optional[ChannelMessage] = None) -> None:
    """处理一条入站消息。出口只有 silent / push_reply / 流式 edit。"""
    if not _is_allowed_chat(channel, chat_id):
        return

    token_channel = _CURRENT_CHANNEL.set(channel)
    token_target = _CURRENT_TARGET.set(str(chat_id))
    token_account = _CURRENT_ACCOUNT.set(account_id)

    def _reset_context() -> None:
        _CURRENT_CHANNEL.reset(token_channel)
        _CURRENT_TARGET.reset(token_target)
        _CURRENT_ACCOUNT.reset(token_account)

    set_req_id(new_req_id())
    started = time.time()
    inbound_id = None
    channel_inbound_id = None
    log.info("INBOUND update_id=%s chat=%s text=%s", update_id, chat_id, text[:100].replace("\n", " "))
    if update_id is not None:
        inbound_id = log_inbound_start(
            update_id=update_id, chat_id=chat_id,
            user_msg_id=user_msg_id or 0, raw_text=text,
        )
        if inbound_id is None:
            log.info("DEDUP update_id=%s 已处理过，跳过", update_id)
            _reset_context()
            return  # 已处理过的 update_id，跳过
    elif channel_message is not None:
        channel_inbound_id = get_default_gateway().log_inbound_start(channel_message, db_path=DB_PATH)
        if channel_inbound_id is None:
            log.info("DEDUP channel message=%s 已处理过，跳过", channel_message.dedupe_key())
            _reset_context()
            return

    def _finish(status: str, response_msg_id: Optional[object] = None, error: Optional[str] = None):
        if inbound_id is not None:
            log_inbound_finish(inbound_id, response_msg_id=response_msg_id,
                               status=status, duration_ms=int((time.time()-started)*1000),
                               error=error)
        elif channel_inbound_id is not None:
            get_default_gateway().log_inbound_finish(
                channel_inbound_id,
                response=_delivery_for_response(response_msg_id),
                status=status,
                duration_ms=int((time.time()-started)*1000),
                error=error,
                db_path=DB_PATH,
            )

    def _parsed(parsed_command: str, parsed_intent: Optional[str] = None,
                parsed_payload: Optional[dict] = None):
        if inbound_id is not None:
            log_inbound_update_parsed(
                inbound_id,
                parsed_command=parsed_command,
                parsed_intent=parsed_intent,
                parsed_payload=parsed_payload,
            )
        elif channel_inbound_id is not None:
            get_default_gateway().log_inbound_update_parsed(
                channel_inbound_id,
                parsed_command=parsed_command,
                parsed_intent=parsed_intent,
                parsed_payload=parsed_payload,
                db_path=DB_PATH,
            )

    # 1. /help：用法说明
    stripped = text.lstrip()
    low = stripped.lower()
    if low in ("/help", "/start", "/?", "help", "帮助"):
        push_reply(HELP_TEXT)
        _finish("ok")
        _reset_context()
        return

    # 1.5. /ask /ask+ 随时分析
    if low.startswith("/ask"):
        parsed = parse_ask_command(stripped)
        if parsed is None:
            push_reply("❌ /ask 后面要带 query，例如 /ask 光伏怎么样")
            _finish("rejected", error="/ask 无 payload")
            _reset_context()
            return
        _parsed(
            "/ask+" if parsed["mode"] == "deep" else "/ask",
            parsed_payload=parsed,
        )
        timeout = skill_timeout_for(parsed["mode"])
        prompt = (f'请使用 stock-ask skill。严格按 SKILL.md 执行。'
                  f' text="{parsed["payload"]}" mode={parsed["mode"]}')
        msg_id = _tg_send(f"🔍 /ask 分析中（约 1–{timeout//60} 分钟）…")
        last_edit = [0.0]

        def on_text(buf: str):
            now = time.time()
            if now - last_edit[0] < EDIT_THROTTLE:
                return
            last_edit[0] = now
            try:
                _tg_edit(msg_id, buf[:TG_MAX_LEN])
            except Exception:
                pass

        def on_tool(name: str):
            try:
                _tg_edit(msg_id, f"🔍 /ask · 工具 {name}…")
            except Exception:
                pass

        try:
            card = run_skill_streaming_generic(
                prompt=prompt, timeout=timeout, on_text=on_text, on_tool=on_tool,
            )
            # 校验 + 渲染
            ok, violations, log_file = _validate_card_for_push(card or "", "stock-ask")
            if violations and CARD_VALIDATOR_MODE == "enforce":
                warn_text = (
                    "⚠️ <b>卡片被拦截（stock-ask）</b>\n"
                    f"含 {len(violations)} 处数据未在 fact pack 中：\n\n"
                    f"<pre>{format_violations(violations)}</pre>\n\n"
                    f"审计日志：{log_file.name if log_file else '-'}"
                )
                _tg_edit(msg_id, warn_text, parse_mode="HTML")
                _finish("blocked", response_msg_id=msg_id,
                        error=f"card_validator blocked {len(violations)} violations")
            else:
                try:
                    _tg_edit(msg_id, md_to_tg_html(card) if card else "（空卡片）", parse_mode="HTML")
                except Exception:
                    push_reply(card or "（空卡片）")
                if violations:  # warn 模式：原卡推完后追一条提示
                    _tg_send(
                        f"⚠️ card_validator [warn] stock-ask {len(violations)} 处可疑数据\n"
                        f"日志：data/card_violations/{log_file.name if log_file else '-'}"
                    )
                _finish("ok", response_msg_id=msg_id)
        except subprocess.TimeoutExpired:
            _tg_edit(msg_id, "❌ 分析超时，请重试或换 /ask+")
            _finish("timeout", response_msg_id=msg_id, error="subprocess timeout")
            _reset_context()
        except Exception as e:
            _tg_edit(msg_id, f"❌ 分析失败：{e}")
            _finish("error", response_msg_id=msg_id, error=str(e))
            _reset_context()
        else:
            _reset_context()
        return

    # 2. /buy /sell 交易流水命令，优先尝试
    if low.startswith(("/buy", "/sell")):
        # 纯 /buy 或 /sell 无参 → 直接给该 side 的帮助
        bare = low.split()
        if len(bare) == 1 and bare[0] in ("/buy", "/sell"):
            push_reply(_trade_usage(bare[0][1:]))
            _finish("ok")
            _reset_context()
            return
        try:
            parsed = parse_trade_command(text)
        except ValueError as e:
            push_reply(f"❌ {e}")
            _finish("rejected", error=str(e))
            _reset_context()
            return
        if parsed is not None:
            try:
                ack = handle_trade(parsed, reply_to_msg_id)
            except Exception as e:
                push_reply(f"⚠️ 落库失败：{e}")
                _finish("error", error=str(e))
                _reset_context()
                return
            push_reply(ack)
            _finish("ok")
            _reset_context()
            return

    kind, val = query.parse_input(text)
    if kind == "unknown":
        _finish("rejected", error="unknown input")
        _reset_context()
        return

    today = today or date.today().isoformat()

    if kind == "name":
        hits = query.lookup_by_name(val)
        if not hits:
            push_reply(_reject(val, "未找到该名称"))
            _finish("rejected", error="name not found")
            _reset_context()
            return
        if len(hits) > 1:
            lines = "\n".join(f"  {c}  {n}" for c, n in hits[:8])
            push_reply(f"❓ 找到多只，请发代码：\n{lines}")
            _finish("rejected", error="ambiguous name")
            _reset_context()
            return
        code = hits[0][0]
    else:
        code = val

    board = query.board_of(code)
    if board is None:
        push_reply(_reject(code, "未找到该代码"))
        _finish("rejected", error="code not found")
        _reset_context()
        return
    if board in ("star", "bse"):
        label = "科创板" if board == "star" else "北交所"
        push_reply(_reject(code, f"暂不支持{label}"))
        _finish("rejected", error=f"unsupported board: {board}")
        _reset_context()
        return
    if query.is_st(code):
        push_reply(_reject(code, "ST 票风险过高，本助手不分析"))
        _finish("rejected", error="ST stock")
        _reset_context()
        return

    mode = "holding" if code in held_codes() else "fresh"

    global _running, _waiting
    if _running and _waiting >= MAX_QUEUE:
        push_reply(f"⏳ {code}\n忙，稍后再问")
        _finish("rejected", error="queue full")
        _reset_context()
        return
    queued = bool(_running)
    initial = (f"🔍 {code} 排队中（前面 {_running + _waiting} 个）…"
               if queued
               else f"🔍 {code} 分析中…（约 30–90 秒）")
    try:
        msg_id = _tg_send(initial)
    except Exception as e:
        log.exception("占位发送失败 code=%s", code)
        _finish("error", error=str(e))
        _reset_context()
        return

    _waiting += 1
    last_edit = [0.0]   # 节流时间戳
    tools_done = [0]    # tool_use 次数
    start_ts = time.time()

    # 友好工具名映射（其它工具走默认）
    TOOL_LABEL = {
        "Bash": "查数据",
        "Read": "读文件",
        "Grep": "搜数据",
        "Glob": "找数据",
    }

    def _throttled_edit(text: str, force: bool = False) -> None:
        now = time.time()
        if not force and now - last_edit[0] < EDIT_THROTTLE:
            return
        last_edit[0] = now
        try:
            _tg_edit(msg_id, text)
        except Exception:
            log.exception("throttled edit 失败 code=%s", code)

    def on_tool(name: str) -> None:
        tools_done[0] += 1
        label = TOOL_LABEL.get(name, name)
        elapsed = int(time.time() - start_ts)
        _throttled_edit(
            f"🔍 {code}\n\n⏳ 已用 {elapsed}s · 第 {tools_done[0]} 步：{label}…"
        )

    def on_text(buf: str) -> None:
        _throttled_edit(f"🔍 {code}\n\n{buf}")

    try:
        with open(LOCK_FILE, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            _waiting -= 1
            _running += 1
            try:
                card = run_skill_streaming(code, mode, on_text, on_tool)
            except subprocess.TimeoutExpired:
                _tg_edit(msg_id, f"⌛ {code}\n分析超时，稍后再试")
                _finish("timeout", response_msg_id=msg_id, error="subprocess timeout")
                return
            except Exception as e:
                _tg_edit(msg_id, f"⚠️ {code}\n分析失败：{e}")
                _finish("error", response_msg_id=msg_id, error=str(e))
                return
            finally:
                _running -= 1
        # 校验
        ok, violations, log_file = _validate_card_for_push(card or "", "stock-query")
        if violations and CARD_VALIDATOR_MODE == "enforce":
            warn_text = (
                f"⚠️ <b>卡片被拦截（stock-query {code}）</b>\n"
                f"含 {len(violations)} 处数据未在 fact pack 中：\n\n"
                f"<pre>{format_violations(violations)}</pre>\n\n"
                f"审计日志：{log_file.name if log_file else '-'}"
            )
            _tg_edit(msg_id, warn_text, parse_mode="HTML")
            _finish("blocked", response_msg_id=msg_id,
                    error=f"card_validator blocked {len(violations)} violations")
        else:
            # 最终卡片：HTML 渲染后落到同一条消息
            try:
                _tg_edit(msg_id, md_to_tg_html(card) if card else "（空卡片）",
                         parse_mode="HTML")
            except Exception:
                log.exception("最终 edit 失败 → 退化为新发 code=%s", code)
                push_reply(card)
            if violations:
                _tg_send(
                    f"⚠️ card_validator [warn] stock-query {len(violations)} 处可疑数据\n"
                    f"日志：data/card_violations/{log_file.name if log_file else '-'}"
                )
            _finish("ok", response_msg_id=msg_id)
    except Exception:
        _waiting = max(0, _waiting - 1)
        raise
    finally:
        _reset_context()


def _is_allowed_chat(channel: str, chat_id) -> bool:
    if channel == "telegram":
        return str(chat_id) == str(ALLOWED_CHAT_ID)
    if channel == "feishu":
        raw = (
            os.environ.get("FEISHU_ALLOWED_CHAT_IDS")
            or os.environ.get("FEISHU_HOME_CHANNEL")
            or os.environ.get("FEISHU_DEFAULT_CHAT_ID", "")
        )
        if raw.strip() == "*":
            return True
        allowed = {x.strip() for x in raw.split(",") if x.strip()}
        return bool(allowed) and str(chat_id) in allowed
    return False


def handle_channel_message(message: ChannelMessage, today: Optional[str] = None) -> None:
    handle(
        message.text,
        chat_id=message.conversation_id,
        today=today,
        update_id=None,
        user_msg_id=None,
        channel=message.channel,
        account_id=message.account_id,
        channel_message=message,
    )


# ============================================================
# TG 长轮询主循环
# ============================================================

def _load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip() or 0)
        except Exception:
            return 0
    return 0


def _save_offset(v: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(v))


def _acquire_poll_lock():
    POLL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(POLL_LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        log.critical("tg_listener already running; refuse duplicate getUpdates poller")
        sys.exit(78)
    lock.write(str(os.getpid()))
    lock.truncate()
    lock.flush()
    return lock


def _get_updates(offset: int) -> list[dict]:
    r = requests.get(
        f"{TG_API}/getUpdates",
        params={"offset": offset, "timeout": 10},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data.get("result") or []


def main() -> None:
    if not TG_TOKEN or not ALLOWED_CHAT_ID:
        log.critical("TG_BOT_TOKEN / ALLOWED_CHAT_ID 未配置，退出")
        sys.exit(2)
    _poll_lock = _acquire_poll_lock()
    offset = _load_offset()
    log.info("启动 offset=%d", offset)
    backoff = 1
    poll_failures = 0
    first_failure_at: float | None = None
    while True:
        try:
            updates = _get_updates(offset)
            if poll_failures:
                downtime = time.monotonic() - (first_failure_at or time.monotonic())
                log.info("getUpdates recovered after %d failures, downtime %.1fs",
                         poll_failures, downtime)
                poll_failures = 0
                first_failure_at = None
            backoff = 1
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                _save_offset(offset)
                msg = u.get("message") or u.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                reply_to = (msg.get("reply_to_message") or {}).get("message_id")
                if not text or chat_id is None:
                    continue
                try:
                    handle(text, chat_id, reply_to_msg_id=reply_to,
                           update_id=u["update_id"], user_msg_id=msg.get("message_id"))
                except Exception:
                    log.exception("handle 异常 update_id=%s", u.get("update_id"))
        except Exception as e:
            poll_failures += 1
            if first_failure_at is None:
                first_failure_at = time.monotonic()
            should_alert = (
                poll_failures == POLL_ALERT_AFTER_FAILURES
                or (
                    poll_failures > POLL_ALERT_AFTER_FAILURES
                    and POLL_ALERT_EVERY_FAILURES > 0
                    and poll_failures % POLL_ALERT_EVERY_FAILURES == 0
                )
            )
            msg = ("getUpdates failed #%d (%s: %s), backoff %ds"
                   % (poll_failures, type(e).__name__, _safe_error_text(e)[:200], backoff))
            if should_alert:
                log.error(msg, exc_info=True)
            else:
                log.warning(msg)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()

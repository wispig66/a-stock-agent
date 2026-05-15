"""TG 长轮询守护进程：接收单股代码/名称 → 调 CC headless 跑 stock-query → 流式回卡片。

并发：fcntl 文件锁 + 排队计数器；1 跑 + 3 等 = 4 容量，第 5 拒绝。
失败重试：TG API 指数退避，CC 子进程超时 180s 直接报错。
进程崩溃由 launchd KeepAlive 拉起；offset 持久化到 data/tg_offset.txt。

流式输出：claude -p --output-format stream-json --include-partial-messages
解析 content_block_delta 累积文本，每 1.5s editMessageText 一次（限速 + 节省 API 调用）。
"""
from __future__ import annotations
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import re
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from lib import query  # noqa: E402
from notify import push_md, md_to_tg_html  # noqa: E402
from db import connect  # noqa: E402

_ASK_RE = re.compile(r"^/ask(\+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)

ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

HOLDINGS_FILE = ROOT / "holdings.yaml"
OFFSET_FILE = ROOT / "data" / "tg_offset.txt"
LOCK_FILE = "/tmp/stock-query.lock"
MAX_QUEUE = 3
_SKILL_TIMEOUT_NORMAL = 180
_SKILL_TIMEOUT_DEEP = 300
SKILL_TIMEOUT = _SKILL_TIMEOUT_NORMAL  # default; per-call override via run_skill_streaming_generic
EDIT_THROTTLE = 1.0         # Telegram editMessageText 限速：≥1s/chat 才安全
TG_MAX_LEN = 4000           # 4096 上限，留 96 字 buffer

_running = 0
_waiting = 0

DB_PATH = ROOT / "data" / "daily.db"


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
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


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


def log_inbound_finish(inbound_id: int, *, response_msg_id: Optional[int],
                       status: str, duration_ms: int, error: Optional[str] = None) -> None:
    with connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE tg_inbound SET response_msg_id=?, handler_status=?, "
            "duration_ms=?, handler_error=? WHERE id=?",
            (response_msg_id, status, duration_ms, error, inbound_id),
        )
        conn.commit()


# ============================================================
# Telegram 低层 API（发送 / 编辑）
# ============================================================

def _tg_send(text: str, parse_mode: Optional[str] = None) -> int:
    """发新消息，返回 message_id。"""
    r = requests.post(f"{TG_API}/sendMessage", json={
        "chat_id": TG_CHAT_ID, "text": text[:TG_MAX_LEN],
        "disable_web_page_preview": True,
        **({"parse_mode": parse_mode} if parse_mode else {}),
    }, timeout=10)
    r.raise_for_status()
    return r.json()["result"]["message_id"]


def _tg_edit(message_id: int, text: str, parse_mode: Optional[str] = None) -> None:
    """编辑已发消息。HTML parse 失败时回退纯文本（流式过程中 markdown 半截不可避免）。"""
    payload = {
        "chat_id": TG_CHAT_ID, "message_id": message_id,
        "text": text[:TG_MAX_LEN], "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(f"{TG_API}/editMessageText", json=payload, timeout=10)
    if r.status_code == 400 and parse_mode:
        # HTML 半截解析失败 → 纯文本重试
        payload.pop("parse_mode", None)
        r = requests.post(f"{TG_API}/editMessageText", json=payload, timeout=10)
    # 忽略 "message is not modified"（内容没变）和 429 限流，不影响主流程
    if r.status_code not in (200, 400, 429):
        r.raise_for_status()


def push_reply(text: str) -> None:
    """非流式回 TG（拒绝卡 / 错误提示 用）。"""
    try:
        push_md(text, source="stock-query")
    except Exception as e:
        print(f"[tg_listener] push 失败: {e}", file=sys.stderr)


# ============================================================
# 业务
# ============================================================

def held_codes() -> set[str]:
    if not HOLDINGS_FILE.exists():
        return set()
    try:
        data = yaml.safe_load(HOLDINGS_FILE.read_text()) or {}
    except Exception:
        return set()
    return {str(h.get("code")).zfill(6) for h in (data.get("holdings") or [])
            if h.get("code")}


def run_skill_streaming(code: str, mode: str,
                        on_text: Callable[[str], None],
                        on_tool: Callable[[str], None]) -> str:
    """流式跑 stock-query skill。

    on_text(accumulated)：模型写卡片时（每个 text_delta）回调，调用方自行节流
    on_tool(tool_name)：每次 tool_use 开始时回调（拉数据进度）
    返回最终完整卡片文本。
    """
    prompt = (f"请使用 stock-query skill 分析这只股票，严格按 SKILL.md "
              f"模板输出卡片，不要任何额外文字：code={code} mode={mode}")
    cmd = ["claude", "-p", "--permission-mode", "bypassPermissions",
           "--output-format", "stream-json",
           "--include-partial-messages",
           "--verbose"]

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdin and proc.stdout
    proc.stdin.write(prompt)
    proc.stdin.close()

    accumulated = ""
    final_text = ""
    start = time.time()

    try:
        for raw in iter(proc.stdout.readline, ""):
            if time.time() - start > SKILL_TIMEOUT:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, SKILL_TIMEOUT)
            line = raw.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")

            # 工具调用：拉数据阶段的进度信号
            if etype == "stream_event":
                ev = evt.get("event", {})
                ev_type = ev.get("type")
                if ev_type == "content_block_start":
                    block = ev.get("content_block", {})
                    if block.get("type") == "tool_use":
                        on_tool(block.get("name", "tool"))
                elif ev_type == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        accumulated += delta.get("text", "")
                        on_text(accumulated)
                continue

            if etype == "assistant":
                content = (evt.get("message") or {}).get("content") or []
                for blk in content:
                    if blk.get("type") == "text":
                        accumulated = blk.get("text", "") or accumulated
                        on_text(accumulated)
                continue

            if etype == "result":
                final_text = (evt.get("result") or "").strip()
                continue
    finally:
        proc.wait(timeout=5)

    if proc.returncode != 0:
        err = proc.stderr.read()[:500] if proc.stderr else ""
        raise RuntimeError(f"claude -p 退出码 {proc.returncode}: {err}")

    return final_text or accumulated.strip()


def run_skill_streaming_generic(*, prompt: str, timeout: int,
                                on_text: Callable[[str], None],
                                on_tool: Callable[[str], None]) -> str:
    """通用流式跑 claude -p。返回最终卡片文本。
    与 run_skill_streaming 的差异：prompt + timeout 都是入参，不依赖模块级 SKILL_TIMEOUT。"""
    cmd = ["claude", "-p", "--permission-mode", "bypassPermissions",
           "--output-format", "stream-json",
           "--include-partial-messages",
           "--verbose"]

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdin and proc.stdout
    proc.stdin.write(prompt)
    proc.stdin.close()

    accumulated = ""
    final_text = ""
    start = time.time()

    try:
        for raw in iter(proc.stdout.readline, ""):
            if time.time() - start > timeout:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)
            line = raw.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "stream_event":
                ev = evt.get("event", {})
                ev_type = ev.get("type")
                if ev_type == "content_block_start":
                    block = ev.get("content_block", {})
                    if block.get("type") == "tool_use":
                        on_tool(block.get("name", "tool"))
                elif ev_type == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        accumulated += delta.get("text", "")
                        on_text(accumulated)
                continue
            if etype == "assistant":
                content = (evt.get("message") or {}).get("content") or []
                for blk in content:
                    if blk.get("type") == "text":
                        accumulated = blk.get("text", "") or accumulated
                        on_text(accumulated)
                continue
            if etype == "result":
                final_text = (evt.get("result") or "").strip()
                continue
    finally:
        proc.wait(timeout=5)

    if proc.returncode != 0:
        err = proc.stderr.read()[:500] if proc.stderr else ""
        raise RuntimeError(f"claude -p 退出码 {proc.returncode}: {err}")

    return final_text or accumulated.strip()


def _reject(code: str, reason: str) -> str:
    return f"❌ {code}\n原因：{reason}"


# ============================================================
# 交易流水：/buy /sell 命令解析 + 落库
# ============================================================

BUY_REASONS = ("二板接力", "龙头补涨", "火箭跟", "自主")
SELL_REASONS = ("止盈", "破位", "跳水", "换股")
TRADES_DB = ROOT / "data" / "daily.db"

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
    return "\n".join(parts)


def handle(text: str, chat_id, today: Optional[str] = None,
           reply_to_msg_id: Optional[int] = None,
           update_id: Optional[int] = None,
           user_msg_id: Optional[int] = None) -> None:
    """处理一条入站消息。出口只有 silent / push_reply / 流式 edit。"""
    if str(chat_id) != str(ALLOWED_CHAT_ID):
        return

    started = time.time()
    inbound_id = None
    if update_id is not None:
        inbound_id = log_inbound_start(
            update_id=update_id, chat_id=chat_id,
            user_msg_id=user_msg_id or 0, raw_text=text,
        )
        if inbound_id is None:
            return  # 已处理过的 update_id，跳过

    def _finish(status: str, response_msg_id: Optional[int] = None, error: Optional[str] = None):
        if inbound_id is not None:
            log_inbound_finish(inbound_id, response_msg_id=response_msg_id,
                               status=status, duration_ms=int((time.time()-started)*1000),
                               error=error)

    # 1. /help：用法说明
    stripped = text.lstrip()
    low = stripped.lower()
    if low in ("/help", "/start", "/?", "help", "帮助"):
        push_reply(HELP_TEXT)
        _finish("ok")
        return

    # 1.5. /ask /ask+ 随时分析
    if low.startswith("/ask"):
        parsed = parse_ask_command(stripped)
        if parsed is None:
            push_reply("❌ /ask 后面要带 query，例如 /ask 光伏怎么样")
            _finish("rejected", error="/ask 无 payload")
            return
        if inbound_id is not None:
            log_inbound_update_parsed(
                inbound_id,
                parsed_command="/ask+" if parsed["mode"] == "deep" else "/ask",
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
            try:
                _tg_edit(msg_id, md_to_tg_html(card) if card else "（空卡片）", parse_mode="HTML")
            except Exception:
                push_reply(card or "（空卡片）")
            _finish("ok", response_msg_id=msg_id)
        except subprocess.TimeoutExpired:
            _tg_edit(msg_id, "❌ 分析超时，请重试或换 /ask+")
            _finish("timeout", response_msg_id=msg_id, error="subprocess timeout")
        except Exception as e:
            _tg_edit(msg_id, f"❌ 分析失败：{e}")
            _finish("error", response_msg_id=msg_id, error=str(e))
        return

    # 2. /buy /sell 交易流水命令，优先尝试
    if low.startswith(("/buy", "/sell")):
        # 纯 /buy 或 /sell 无参 → 直接给该 side 的帮助
        bare = low.split()
        if len(bare) == 1 and bare[0] in ("/buy", "/sell"):
            push_reply(_trade_usage(bare[0][1:]))
            _finish("ok")
            return
        try:
            parsed = parse_trade_command(text)
        except ValueError as e:
            push_reply(f"❌ {e}")
            _finish("rejected", error=str(e))
            return
        if parsed is not None:
            try:
                ack = handle_trade(parsed, reply_to_msg_id)
            except Exception as e:
                push_reply(f"⚠️ 落库失败：{e}")
                _finish("error", error=str(e))
                return
            push_reply(ack)
            _finish("ok")
            return

    kind, val = query.parse_input(text)
    if kind == "unknown":
        _finish("rejected", error="unknown input")
        return

    today = today or date.today().isoformat()

    if kind == "name":
        hits = query.lookup_by_name(val)
        if not hits:
            push_reply(_reject(val, "未找到该名称"))
            _finish("rejected", error="name not found")
            return
        if len(hits) > 1:
            lines = "\n".join(f"  {c}  {n}" for c, n in hits[:8])
            push_reply(f"❓ 找到多只，请发代码：\n{lines}")
            _finish("rejected", error="ambiguous name")
            return
        code = hits[0][0]
    else:
        code = val

    board = query.board_of(code)
    if board is None:
        push_reply(_reject(code, "未找到该代码"))
        _finish("rejected", error="code not found")
        return
    if board in ("star", "bse"):
        label = "科创板" if board == "star" else "北交所"
        push_reply(_reject(code, f"暂不支持{label}"))
        _finish("rejected", error=f"unsupported board: {board}")
        return
    if query.is_st(code):
        push_reply(_reject(code, "ST 票风险过高，本助手不分析"))
        _finish("rejected", error="ST stock")
        return

    mode = "holding" if code in held_codes() else "fresh"

    global _running, _waiting
    if _running and _waiting >= MAX_QUEUE:
        push_reply(f"⏳ {code}\n忙，稍后再问")
        _finish("rejected", error="queue full")
        return
    queued = bool(_running)
    initial = (f"🔍 {code} 排队中（前面 {_running + _waiting} 个）…"
               if queued
               else f"🔍 {code} 分析中…（约 30–90 秒）")
    try:
        msg_id = _tg_send(initial)
    except Exception as e:
        print(f"[tg_listener] 占位发送失败: {e}", file=sys.stderr)
        _finish("error", error=str(e))
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
        except Exception as e:
            print(f"[tg_listener] edit 失败: {e}", file=sys.stderr)

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
        # 最终卡片：HTML 渲染后落到同一条消息
        try:
            _tg_edit(msg_id, md_to_tg_html(card) if card else "（空卡片）",
                     parse_mode="HTML")
        except Exception as e:
            print(f"[tg_listener] 最终 edit 失败 → 退化为新发: {e}",
                  file=sys.stderr)
            push_reply(card)
        _finish("ok", response_msg_id=msg_id)
    except Exception:
        _waiting = max(0, _waiting - 1)
        raise


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
        print("[tg_listener] TG_BOT_TOKEN / ALLOWED_CHAT_ID 未配置", file=sys.stderr)
        sys.exit(2)
    offset = _load_offset()
    print(f"[tg_listener] start, offset={offset}", flush=True)
    backoff = 1
    while True:
        try:
            updates = _get_updates(offset)
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
                except Exception as e:
                    print(f"[tg_listener] handle 异常: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[tg_listener] loop 异常: {e}; 退避 {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()

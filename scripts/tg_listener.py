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
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from lib import query  # noqa: E402
from notify import push_md, md_to_tg_html  # noqa: E402

ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

HOLDINGS_FILE = ROOT / "holdings.yaml"
OFFSET_FILE = ROOT / "data" / "tg_offset.txt"
LOCK_FILE = "/tmp/stock-query.lock"
MAX_QUEUE = 3
SKILL_TIMEOUT = 180
EDIT_THROTTLE = 1.0         # Telegram editMessageText 限速：≥1s/chat 才安全
TG_MAX_LEN = 4000           # 4096 上限，留 96 字 buffer

_running = 0
_waiting = 0


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


def _reject(code: str, reason: str) -> str:
    return f"❌ {code}\n原因：{reason}"


def handle(text: str, chat_id, today: Optional[str] = None) -> None:
    """处理一条入站消息。出口只有 silent / push_reply / 流式 edit。"""
    if str(chat_id) != str(ALLOWED_CHAT_ID):
        return

    kind, val = query.parse_input(text)
    if kind == "unknown":
        return

    today = today or date.today().isoformat()

    if kind == "name":
        hits = query.lookup_by_name(val)
        if not hits:
            push_reply(_reject(val, "未找到该名称"))
            return
        if len(hits) > 1:
            lines = "\n".join(f"  {c}  {n}" for c, n in hits[:8])
            push_reply(f"❓ 找到多只，请发代码：\n{lines}")
            return
        code = hits[0][0]
    else:
        code = val

    board = query.board_of(code)
    if board is None:
        push_reply(_reject(code, "未找到该代码"))
        return
    if board in ("star", "bse"):
        label = "科创板" if board == "star" else "北交所"
        push_reply(_reject(code, f"暂不支持{label}"))
        return
    if query.is_st(code):
        push_reply(_reject(code, "ST 票风险过高，本助手不分析"))
        return

    mode = "holding" if code in held_codes() else "fresh"

    global _running, _waiting
    if _running and _waiting >= MAX_QUEUE:
        push_reply(f"⏳ {code}\n忙，稍后再问")
        return
    queued = bool(_running)
    initial = (f"🔍 {code} 排队中（前面 {_running + _waiting} 个）…"
               if queued
               else f"🔍 {code} 分析中…（约 30–90 秒）")
    try:
        msg_id = _tg_send(initial)
    except Exception as e:
        print(f"[tg_listener] 占位发送失败: {e}", file=sys.stderr)
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
                return
            except Exception as e:
                _tg_edit(msg_id, f"⚠️ {code}\n分析失败：{e}")
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
                if not text or chat_id is None:
                    continue
                try:
                    handle(text, chat_id)
                except Exception as e:
                    print(f"[tg_listener] handle 异常: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[tg_listener] loop 异常: {e}; 退避 {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()

"""TG 长轮询守护进程：接收单股代码/名称 → 调 CC headless 跑 stock-query → 回卡片。

并发：fcntl 文件锁 + 排队计数器；1 跑 + 3 等 = 4 容量，第 5 拒绝。
失败重试：TG API 指数退避，CC 子进程超时 180s 直接报错。
进程崩溃由 launchd KeepAlive 拉起；offset 持久化到 data/tg_offset.txt。
"""
from __future__ import annotations
import fcntl
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from lib import query  # noqa: E402
from notify import push_md  # noqa: E402

ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

HOLDINGS_FILE = ROOT / "holdings.yaml"
OFFSET_FILE = ROOT / "data" / "tg_offset.txt"
LOCK_FILE = "/tmp/stock-query.lock"
MAX_QUEUE = 3
SKILL_TIMEOUT = 180

_running = 0
_waiting = 0


def push_reply(text: str) -> None:
    """回 TG。"""
    try:
        push_md(text, source="stock-query")
    except Exception as e:
        print(f"[tg_listener] push 失败: {e}", file=sys.stderr)


def held_codes() -> set[str]:
    if not HOLDINGS_FILE.exists():
        return set()
    try:
        data = yaml.safe_load(HOLDINGS_FILE.read_text()) or {}
    except Exception:
        return set()
    return {str(h.get("code")).zfill(6) for h in (data.get("holdings") or [])
            if h.get("code")}


def run_skill(code: str, mode: str) -> str:
    """通过 claude -p headless 跑 stock-query skill；返回 markdown。"""
    prompt = (f"请使用 stock-query skill 分析这只股票，严格按 SKILL.md "
              f"模板输出卡片，不要任何额外文字：code={code} mode={mode}")
    proc = subprocess.run(
        ["claude", "-p", "--permission-mode", "bypassPermissions"],
        cwd=str(ROOT),
        input=prompt, capture_output=True, text=True, timeout=SKILL_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p 退出码 {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout.strip()


def _reject(code: str, reason: str) -> str:
    return f"❌ {code}\n原因：{reason}"


def handle(text: str, chat_id, today: Optional[str] = None) -> None:
    """处理一条入站消息。出口只有 silent / push_reply。"""
    if str(chat_id) != str(ALLOWED_CHAT_ID):
        return

    kind, val = query.parse_input(text)
    if kind == "unknown":
        return  # 闲聊/纯数字非6位/空串 → 静默

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
    # 注：停牌检测下推到 skill 内（用实时盘口判定）。基于 daily_kline 的判定
    # 在盘前/盘中不可靠——当日 kline 要等盘后 download_daily 才入库。

    mode = "holding" if code in held_codes() else "fresh"

    global _running, _waiting
    if _running and _waiting >= MAX_QUEUE:
        push_reply(f"⏳ {code}\n忙，稍后再问")
        return
    queued = bool(_running)
    if queued:
        push_reply(f"🔍 {code} 排队中（前面还有 {_running + _waiting} 个），稍候…")
    else:
        push_reply(f"🔍 {code} 分析中…（约 30–90 秒）")
    _waiting += 1
    try:
        with open(LOCK_FILE, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            _waiting -= 1
            _running += 1
            try:
                card = run_skill(code, mode)
            except subprocess.TimeoutExpired:
                push_reply(f"⌛ {code}\n分析超时，稍后再试")
                return
            except Exception as e:
                push_reply(f"⚠️ {code}\n分析失败：{e}")
                return
            finally:
                _running -= 1
        push_reply(card)
    except Exception:
        # 如果锁未拿到（理论上不会，flock 是阻塞的）回滚 _waiting
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

"""统一日志库。

用法：
    from stock_codex.infra.logger import get_logger, new_req_id, set_req_id, run_subprocess

    log = get_logger("command_router")
    log.info("启动")
    try:
        ...
    except Exception:
        log.exception("xxx 失败")   # 自动带 traceback

req_id 贯穿：
    set_req_id(new_req_id())           # TG 入站时
    run_subprocess(["codex", "exec", ...], name="ask")  # 自动把 req_id 塞子进程 env
    # 子进程脚本启动时：
    from stock_codex.infra.logger import init_req_id_from_env
    init_req_id_from_env()

ERROR/CRITICAL 自动推 TG（同 daemon+异常类型 5min 节流）。
"""
from __future__ import annotations

import contextvars
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from typing import Iterable

from stock_codex.paths import LOG_DIR

LOG_DIR.mkdir(exist_ok=True)

_req_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("req_id", default="-")
_loggers: dict[str, logging.Logger] = {}
_in_tg_handler = False  # 防递归


def _redact_secrets(text: str) -> str:
    for key in ("FEISHU_APP_SECRET", "WEIXIN_TOKEN"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"/bot[^/\s]+", "/bot<redacted>", text)
    return text


def new_req_id() -> str:
    return uuid.uuid4().hex[:8]


def set_req_id(rid: str) -> None:
    _req_id_var.set(rid)


def get_req_id() -> str:
    return _req_id_var.get()


def init_req_id_from_env() -> None:
    rid = os.environ.get("STOCK_REQ_ID")
    if rid:
        _req_id_var.set(rid)


class _ReqIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.req_id = _req_id_var.get()
        return True


class _TgErrorHandler(logging.Handler):
    """ERROR+ → TG 推送，按 (logger_name, exception_type) 5 分钟节流。"""

    def __init__(self, throttle_sec: int = 300):
        super().__init__(level=logging.ERROR)
        self.throttle_sec = throttle_sec
        self._last_sent: dict[tuple[str, str], float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        global _in_tg_handler
        if _in_tg_handler:
            return
        exc_type = "NoExc"
        if record.exc_info and record.exc_info[0]:
            exc_type = record.exc_info[0].__name__
        key = (record.name, exc_type)
        now = time.time()
        last = self._last_sent.get(key, 0)
        if now - last < self.throttle_sec:
            return
        self._last_sent[key] = now

        lines = [
            f"🚨 [{record.name}] {record.levelname}",
            f"req={getattr(record, 'req_id', '-')}  exc={exc_type}",
            f"msg: {_redact_secrets(record.getMessage())[:300]}",
        ]
        if record.exc_info:
            tb = "".join(traceback.format_exception(*record.exc_info))
            tail = "\n".join(tb.strip().splitlines()[-6:])
            lines.append(f"```\n{_redact_secrets(tail)[:800]}\n```")
        text = "\n".join(lines)

        _in_tg_handler = True
        try:
            from stock_codex.infra.notify import push  # type: ignore
            push(text, source=f"error:{record.name}", raw=True)
        except Exception as e:
            print(f"[logger:TgErrorHandler] 推送失败: {e}", file=sys.stderr, flush=True)
        finally:
            _in_tg_handler = False


def get_logger(name: str, *, level: int = logging.INFO, tg_alert: bool = True) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    log = logging.getLogger(name)
    log.setLevel(level)
    log.propagate = False
    log.addFilter(_ReqIdFilter())

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] [req=%(req_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件 handler: 日切，保留 30 天
    log_path = LOG_DIR / f"{name}.log"
    fh = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.suffix = "%Y-%m-%d"
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # stderr handler: WARNING+
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    if tg_alert:
        log.addHandler(_TgErrorHandler())

    _loggers[name] = log
    return log


def run_subprocess(
    cmd: Iterable[str],
    *,
    name: str,
    timeout: float | None = None,
    env_extra: dict[str, str] | None = None,
    input_text: str | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess:
    """子进程包装：自动注入 req_id env、capture_output、stderr 落盘、失败时 log.error。

    name: 子进程标签，用于日志区分（如 "ask", "ask_generic", "premarket_skill"）
    """
    log = get_logger(f"subprocess.{name}")
    env = os.environ.copy()
    env["STOCK_REQ_ID"] = get_req_id()
    if env_extra:
        env.update(env_extra)

    cmd_list = list(cmd)
    log.info("启动: %s (timeout=%s)", " ".join(cmd_list[:6]) + (" ..." if len(cmd_list) > 6 else ""), timeout)
    t0 = time.time()
    try:
        r = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            input=input_text,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as e:
        log.exception("超时 (%.1fs): %s", time.time() - t0, cmd_list[0])
        raise
    except Exception:
        log.exception("启动失败: %s", cmd_list[0])
        raise

    dt = time.time() - t0
    if r.returncode != 0:
        log.error(
            "退出码 %d (%.1fs)\nstdout(tail):\n%s\nstderr(tail):\n%s",
            r.returncode, dt,
            (r.stdout or "")[-2000:],
            (r.stderr or "")[-2000:],
        )
    else:
        log.info("完成 rc=0 (%.1fs)", dt)
        if r.stderr:
            log.debug("stderr: %s", r.stderr[:500])
    return r

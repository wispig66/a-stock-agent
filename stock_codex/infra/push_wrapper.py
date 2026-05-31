"""Unified outbound card push path.

This module backs the skill-level ``push.py`` CLI and long-running daemons.
It keeps validation, audit logging, machine-block stripping, chunking, and
final channel delivery on one path.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from stock_codex.infra.notify import push

ROOT = Path(__file__).resolve().parents[2]

CHUNK_LIMIT = 3800
DB_PATH = ROOT / "data" / "daily.db"
AUTOMATION_ENFORCE_SOURCES = {
    "stock-premarket",
    "stock-intraday",
    "stock-postmarket",
    "stock-weekly",
}
MACHINE_BLOCK_RE = re.compile(r"\n?```decision_tickets\s*.*?```\n?", re.DOTALL)


class PushBlocked(RuntimeError):
    def __init__(
        self,
        source: str,
        mode: str,
        violations: list,
        log_file: Path,
        *,
        notified: bool = False,
    ) -> None:
        self.source = source
        self.mode = mode
        self.violations = violations
        self.log_file = log_file
        self.notified = notified
        super().__init__(
            f"card blocked by validator source={source} mode={mode} "
            f"violations={len(violations)} log={log_file}"
        )


def validator_mode(source: str) -> str:
    explicit = os.environ.get("CARD_VALIDATOR_MODE")
    if explicit:
        return explicit.lower()
    if source in AUTOMATION_ENFORCE_SOURCES:
        return "enforce"
    return "warn"


def _validate(text: str, source: str, mode: str) -> tuple[bool, list]:
    """Validate text against data/allowed_latest_<source>.json if present."""
    if mode == "off":
        return (True, [])
    allowed_file = ROOT / "data" / f"allowed_latest_{source}.json"
    if not allowed_file.exists():
        return (True, [])
    try:
        from stock_codex.market.card_validator import load_stock_name_dict, validate_card

        allowed = json.loads(allowed_file.read_text(encoding="utf-8"))
        name_dict = load_stock_name_dict(DB_PATH) if DB_PATH.exists() else None
        return validate_card(text, allowed, stock_name_dict=name_dict)
    except Exception as e:
        print(f"[push] validator 异常（fail-open）：{e}", file=sys.stderr, flush=True)
        return (True, [])


def _log_violations(text: str, source: str, mode: str, violations: list) -> Path:
    log_dir = ROOT / "data" / "card_violations"
    log_dir.mkdir(parents=True, exist_ok=True)
    fn = log_dir / f"{int(datetime.now().timestamp())}_{source}.json"
    fn.write_text(
        json.dumps(
            {
                "ts": datetime.now().isoformat(),
                "source": source,
                "mode": mode,
                "card_text": text,
                "violations": [v.to_dict() for v in violations],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return fn


def strip_machine_blocks(text: str) -> str:
    """Remove machine-readable blocks that should be stored but not pushed."""
    return re.sub(r"\n{3,}", "\n\n", MACHINE_BLOCK_RE.sub("\n\n", text))


def split_chunks(text: str) -> list[str]:
    """Split by paragraphs first to avoid hard-cutting Telegram messages."""
    if len(text) <= CHUNK_LIMIT:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        para_len = len(para) + 2
        if size + para_len > CHUNK_LIMIT and buf:
            chunks.append("\n\n".join(buf))
            buf, size = [para], para_len
        else:
            buf.append(para)
            size += para_len
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def validate_for_push(text: str, source: str, mode: str | None = None) -> tuple[bool, list, Path | None, str]:
    mode = mode or validator_mode(source)
    ok, violations = _validate(text, source, mode)
    if not ok:
        return ok, violations, _log_violations(text, source, mode, violations), mode
    return True, [], None, mode


def push_text(text: str, *, source: str = "stock-premarket", notify_blocked: bool = False) -> list[dict]:
    """Validate, chunk, and send text through the project notification gateway."""
    text = strip_machine_blocks(text).strip()
    if not text:
        raise ValueError("empty message")

    ok, violations, log_file, mode = validate_for_push(text, source)
    if not ok:
        assert log_file is not None
        if mode == "enforce":
            notified = False
            if notify_blocked:
                from stock_codex.market.card_validator import format_violations

                summary = format_violations(violations)
                warn_card = (
                    f"⚠️ <b>卡片被拦截（{source}）</b>\n"
                    f"含 {len(violations)} 处数据未在 fact pack 中：\n\n"
                    f"{summary}\n\n"
                    f"审计日志：data/card_violations/{log_file.name}\n"
                    f"原卡未推送。请检查 pipeline 输出或调整 SKILL.md。"
                )
                push(warn_card, source=f"{source}-blocked")
                notified = True
            raise PushBlocked(source, mode, violations, log_file, notified=notified)

    results: list[dict] = []
    chunks = split_chunks(text)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"({i}/{len(chunks)})\n" if len(chunks) > 1 else ""
        results.append(push(prefix + chunk, source=source))
    return results


def push_one(text: str, *, source: str = "stock-premarket", notify_blocked: bool = False) -> dict:
    """Compatibility helper for callers that expect one Telegram response dict."""
    return push_text(text, source=source, notify_blocked=notify_blocked)[0]

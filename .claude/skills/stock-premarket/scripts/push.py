"""
Skill 内置推送 wrapper：从 stdin 或 --text 接收消息，调用项目 code/notify.py。
自动按 Telegram 4096 上限分段（保留 200 字符余量）。

推送前先过 card_validator：卡片里的数据点必须在 data/allowed_latest_<source>.json
里。env CARD_VALIDATOR_MODE ∈ {warn, enforce, off}。
无人值守定时卡默认 enforce；其他来源默认 warn（只写审计日志不拦截）。

用法：
    echo "今日观察池..." | python push.py --source stock-intraday
    python push.py --text "短消息" --source stock-premarket
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "code"))

from notify import push  # noqa: E402

CHUNK_LIMIT = 3800
DB_PATH = ROOT / "data" / "daily.db"
AUTOMATION_ENFORCE_SOURCES = {
    "stock-intraday",
    "stock-postmarket",
    "stock-weekly",
}
MACHINE_BLOCK_RE = re.compile(r"\n?```decision_tickets\s*.*?```\n?", re.DOTALL)


def validator_mode(source: str) -> str:
    explicit = os.environ.get("CARD_VALIDATOR_MODE")
    if explicit:
        return explicit.lower()
    if source in AUTOMATION_ENFORCE_SOURCES:
        return "enforce"
    return "warn"


def _validate(text: str, source: str, mode: str) -> tuple[bool, list]:
    """读 data/allowed_latest_<source>.json 校验 text。无 allowed 文件 = 跳过校验。"""
    if mode == "off":
        return (True, [])
    allowed_file = ROOT / "data" / f"allowed_latest_{source}.json"
    if not allowed_file.exists():
        return (True, [])  # 该 skill 还没接 ALLOWED 体系，跳过（向后兼容）
    try:
        from lib.card_validator import validate_card, load_stock_name_dict
        allowed = json.loads(allowed_file.read_text(encoding="utf-8"))
        name_dict = load_stock_name_dict(DB_PATH) if DB_PATH.exists() else None
        return validate_card(text, allowed, stock_name_dict=name_dict)
    except Exception as e:
        print(f"[push] validator 异常（fail-open）：{e}", file=sys.stderr)
        return (True, [])


def _log_violations(text: str, source: str, mode: str, violations: list) -> Path:
    """落审计日志到 data/card_violations/<ts>_<source>.json。"""
    log_dir = ROOT / "data" / "card_violations"
    log_dir.mkdir(parents=True, exist_ok=True)
    fn = log_dir / f"{int(datetime.now().timestamp())}_{source}.json"
    fn.write_text(json.dumps({
        "ts": datetime.now().isoformat(),
        "source": source,
        "mode": mode,
        "card_text": text,
        "violations": [v.to_dict() for v in violations],
    }, ensure_ascii=False, indent=2))
    return fn


def strip_machine_blocks(text: str) -> str:
    """Remove machine-readable blocks that should be stored but not pushed."""
    return re.sub(r"\n{3,}", "\n\n", MACHINE_BLOCK_RE.sub("\n\n", text))


def split_chunks(text: str) -> list[str]:
    """按段落优先分段，避免硬截断。"""
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


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser()
    p.add_argument("--text", help="消息内容直传")
    p.add_argument("--file", help="从文件读消息内容")
    p.add_argument("--source", default="stock-premarket",
                   help="入库时记录的来源标签，默认 stock-premarket")
    p.add_argument("--notify-blocked", action="store_true",
                   help="enforce 拦截时也推送一条拦截告警；默认只打印错误并退出")
    args = p.parse_args(argv)
    mode = validator_mode(args.source)

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        text = sys.stdin.read()
    text = strip_machine_blocks(text).strip()
    if not text:
        print("ERROR: 空消息，未发送", file=sys.stderr)
        sys.exit(1)

    # 校验
    ok, violations = _validate(text, args.source, mode)
    if not ok:
        from lib.card_validator import format_violations
        log_file = _log_violations(text, args.source, mode, violations)
        summary = format_violations(violations)
        print(f"[push] ⚠️ 卡片含 {len(violations)} 处数据来源违规 "
              f"(mode={mode}) 日志={log_file.name}\n{summary}",
              file=sys.stderr)
        if mode == "enforce":
            if args.notify_blocked:
                warn_card = (
                    f"⚠️ <b>卡片被拦截（{args.source}）</b>\n"
                    f"含 {len(violations)} 处数据未在 fact pack 中：\n\n"
                    f"{summary}\n\n"
                    f"审计日志：data/card_violations/{log_file.name}\n"
                    f"原卡未推送。请检查 pipeline 输出或调整 SKILL.md。"
                )
                r = push(warn_card, source=f"{args.source}-blocked")
                print(f"[push] enforce 模式：原卡已拒推；告警卡 msg_id="
                      f"{r['result']['message_id']}")
            else:
                print("[push] enforce 模式：原卡已拒推，未发送 Telegram",
                      file=sys.stderr)
            sys.exit(2)
        # warn 模式：继续推但已留下违规日志

    chunks = split_chunks(text)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"({i}/{len(chunks)})\n" if len(chunks) > 1 else ""
        r = push(prefix + chunk, source=args.source)
        print(f"chunk {i}/{len(chunks)} msg_id={r['result']['message_id']}")


if __name__ == "__main__":
    main()

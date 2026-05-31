"""
Skill push CLI: validate, chunk, and send a card through the project gateway.

The implementation lives in stock_codex.infra.push_wrapper so unattended Codex
automations and long-running daemons share the same outbound path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stock_codex.infra.push_wrapper import (  # noqa: F401
    ROOT,
    PushBlocked,
    _validate,
    push,
    push_text,
    split_chunks,
    strip_machine_blocks,
    validate_for_push,
    validator_mode,
)


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser()
    p.add_argument("--text", help="消息内容直传")
    p.add_argument("--file", help="从文件读消息内容")
    p.add_argument("--source", default="stock-premarket",
                   help="入库时记录的来源标签，默认 stock-premarket")
    p.add_argument("--notify-blocked", action="store_true",
                   help="enforce 拦截时也推送一条拦截告警；默认只打印错误并退出")
    args = p.parse_args(argv)

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        text = sys.stdin.read()

    try:
        results = push_text(text, source=args.source, notify_blocked=args.notify_blocked)
    except ValueError:
        print("ERROR: 空消息，未发送", file=sys.stderr)
        sys.exit(1)
    except PushBlocked as e:
        from stock_codex.market.card_validator import format_violations

        summary = format_violations(e.violations)
        print(
            f"[push] ⚠️ 卡片含 {len(e.violations)} 处数据来源违规 "
            f"(mode={e.mode}) 日志={e.log_file.name}\n{summary}",
            file=sys.stderr,
        )
        if e.notified:
            print("[push] enforce 模式：原卡已拒推；已发送拦截告警", file=sys.stderr)
        else:
            print("[push] enforce 模式：原卡已拒推，未发送 Telegram", file=sys.stderr)
        sys.exit(2)

    for i, r in enumerate(results, 1):
        print(f"chunk {i}/{len(results)} msg_id={r['result']['message_id']}")


if __name__ == "__main__":
    main()

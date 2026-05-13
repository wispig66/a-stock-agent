"""
Skill 内置推送 wrapper：从 stdin 或 --text 接收消息，调用项目 code/notify.py。
自动按 Telegram 4096 上限分段（保留 200 字符余量）。

用法：
    echo "今日观察池..." | python push.py
    python push.py --text "短消息"
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "code"))

from notify import push  # noqa: E402

CHUNK_LIMIT = 3800


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--text", help="消息内容直传")
    p.add_argument("--file", help="从文件读消息内容")
    p.add_argument("--source", default="stock-premarket",
                   help="入库时记录的来源标签，默认 stock-premarket")
    args = p.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        print("ERROR: 空消息，未发送", file=sys.stderr)
        sys.exit(1)

    chunks = split_chunks(text)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"({i}/{len(chunks)})\n" if len(chunks) > 1 else ""
        r = push(prefix + chunk, source=args.source)
        print(f"chunk {i}/{len(chunks)} msg_id={r['result']['message_id']}")


if __name__ == "__main__":
    main()

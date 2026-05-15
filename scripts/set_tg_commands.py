"""注册 Telegram bot 命令菜单（/ 输入框自动补全）。

一次性运行：`uv run scripts/set_tg_commands.py`
调用 setMyCommands，命令清单持久化在 bot 上，所有客户端立即生效。
改命令或描述后重跑即可。
"""
from __future__ import annotations
import os
import sys

import requests

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
if not TG_TOKEN:
    print("TG_BOT_TOKEN 未配置", file=sys.stderr)
    sys.exit(2)

COMMANDS = [
    {"command": "buy",  "description": "记买入：/buy 代码 价格 手数 [理由] [@HH:MM]"},
    {"command": "sell", "description": "记卖出：/sell 代码 价格 手数 [理由] [@HH:MM]"},
    {"command": "help", "description": "用法说明 + 8 个理由标签速查"},
]


def main() -> None:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/setMyCommands"
    r = requests.post(url, json={"commands": COMMANDS}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        print(f"设置失败: {data}", file=sys.stderr)
        sys.exit(1)
    print("✅ 已注册 bot 命令：")
    for c in COMMANDS:
        print(f"  /{c['command']:<6} {c['description']}")


if __name__ == "__main__":
    main()

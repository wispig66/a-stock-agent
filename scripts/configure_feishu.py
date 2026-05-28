#!/usr/bin/env python3
"""Interactive Feishu/Lark gateway setup.

This keeps the user-facing config close to Hermes' setup flow: ask for a few
required values, validate credentials, and persist sane defaults.
"""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
FEISHU_API = "https://open.feishu.cn/open-apis"


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Feishu gateway env values")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument("--send-test", action="store_true", help="send a test message to FEISHU_HOME_CHANNEL")
    args = parser.parse_args()

    if importlib.util.find_spec("lark_oapi") is None:
        print("✗ lark-oapi 未安装；请先运行：uv sync 或 uv add lark-oapi", file=sys.stderr)
        sys.exit(2)

    env = _read_env(args.env_file)
    app_id = _prompt("FEISHU_APP_ID", env.get("FEISHU_APP_ID", ""))
    app_secret = _prompt_secret("FEISHU_APP_SECRET", env.get("FEISHU_APP_SECRET", ""))
    home_channel = _prompt("FEISHU_HOME_CHANNEL", env.get("FEISHU_HOME_CHANNEL", ""))
    allowed_chats_default = env.get("FEISHU_ALLOWED_CHAT_IDS") or home_channel
    allowed_chats = _prompt("FEISHU_ALLOWED_CHAT_IDS", allowed_chats_default)

    token = _fetch_tenant_token(app_id, app_secret)
    print("✓ Feishu tenant_access_token 校验通过")

    updates = {
        "FEISHU_ENABLED": "1",
        "FEISHU_APP_ID": app_id,
        "FEISHU_APP_SECRET": app_secret,
        "FEISHU_HOME_CHANNEL": home_channel,
        "FEISHU_ALLOWED_CHAT_IDS": allowed_chats,
        "FEISHU_DOMAIN": env.get("FEISHU_DOMAIN") or "feishu",
        "FEISHU_CONNECTION_MODE": "websocket",
        "FEISHU_REQUIRE_MENTION": env.get("FEISHU_REQUIRE_MENTION") or "true",
        "FEISHU_REACTIONS": "false",
    }
    _write_env(args.env_file, env, updates)
    print(f"✓ 已写入 {args.env_file}")

    if args.send_test:
        _send_test(token, home_channel)
        print("✓ 测试消息已发送")
    else:
        print("未发送测试消息；需要时可运行：uv run python scripts/configure_feishu.py --send-test")

    print("\nFeishu 控制台最小检查项：")
    print("- 凭证与基础信息：App ID / App Secret 与 .env 一致")
    print("- 事件订阅：启用机器人接收消息事件 im.message.receive_v1")
    print("- 连接方式：启用长连接 / WebSocket")
    print("- 权限：允许机器人发送和接收消息；把机器人加入目标群或私聊")


def _prompt(name: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{name}{suffix}: ").strip()
    value = value or default
    if not value:
        print(f"✗ {name} 不能为空", file=sys.stderr)
        sys.exit(2)
    return value


def _prompt_secret(name: str, default: str) -> str:
    suffix = " [已存在]" if default else ""
    value = getpass.getpass(f"{name}{suffix}: ").strip()
    value = value or default
    if not value:
        print(f"✗ {name} 不能为空", file=sys.stderr)
        sys.exit(2)
    return value


def _fetch_tenant_token(app_id: str, app_secret: str) -> str:
    r = requests.post(
        f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=10,
    )
    data = r.json()
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise SystemExit(f"✗ Feishu token 校验失败：code={data.get('code')} msg={data.get('msg')}")
    return str(data["tenant_access_token"])


def _send_test(token: str, chat_id: str) -> None:
    import json

    r = requests.post(
        f"{FEISHU_API}/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": "stock-codex Feishu gateway configured."}, ensure_ascii=False),
        },
        timeout=10,
    )
    data = r.json()
    if data.get("code") != 0:
        raise SystemExit(f"✗ 测试消息发送失败：code={data.get('code')} msg={data.get('msg')}")


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _write_env(path: Path, existing: dict[str, str], updates: dict[str, str]) -> None:
    merged = {**existing, **updates}
    lines: list[str] = []
    if path.exists():
        handled: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in merged:
                lines.append(f"{key}={_quote_env(merged[key])}")
                handled.add(key)
            else:
                lines.append(line)
        for key in updates:
            if key not in handled:
                lines.append(f"{key}={_quote_env(merged[key])}")
    else:
        lines.extend(f"{key}={_quote_env(value)}" for key, value in updates.items())
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _quote_env(value: str) -> str:
    if any(ch.isspace() for ch in value) or "#" in value:
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


if __name__ == "__main__":
    os.chdir(ROOT)
    main()

#!/usr/bin/env python3
"""Interactive WeChat personal (iLink Bot) scan-login + env setup.

Mirrors Hermes' weixin flow: request a login QR, wait for the user to scan it
with the WeChat mobile app, then persist the bot_token / base_url to .env.

NOTE: iLink login endpoints need a real network round-trip to validate; this
flow is best-effort and should be verified against a live account.
"""
from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
DEFAULT_BASE = "https://ilinkai.weixin.qq.com"


def _headers(token: str = "") -> dict[str, str]:
    uin = base64.b64encode(str(secrets.randbits(32)).encode()).decode()
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat iLink scan-login + env setup")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--qr-out", type=Path, default=ROOT / "data" / "weixin_login_qr.png")
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for scan confirm")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")

    # 1. request login QR
    r = requests.get(f"{base}/ilink/bot/get_bot_qrcode", params={"bot_type": 3},
                     headers=_headers(), timeout=15)
    r.raise_for_status()
    data = r.json()
    qrcode = data.get("qrcode")
    img_b64 = data.get("qrcode_img_content")
    if not qrcode:
        raise SystemExit(f"✗ 获取二维码失败：{data}")
    if img_b64:
        args.qr_out.parent.mkdir(parents=True, exist_ok=True)
        try:
            args.qr_out.write_bytes(base64.b64decode(img_b64))
            print(f"✓ 登录二维码已保存：{args.qr_out}")
            print("  用微信扫码并在手机上确认登录…")
        except Exception:
            print("⚠️ 二维码图片解码失败，请改用其它方式获取二维码")
    print(f"  qrcode={qrcode}")

    # 2. poll scan status
    deadline = time.time() + args.timeout
    bot_token = ""
    baseurl = base
    while time.time() < deadline:
        time.sleep(2)
        sr = requests.get(f"{base}/ilink/bot/get_qrcode_status", params={"qrcode": qrcode},
                          headers=_headers(), timeout=15)
        sr.raise_for_status()
        sd = sr.json()
        status = str(sd.get("status") or "")
        if status == "confirmed" and sd.get("bot_token"):
            bot_token = str(sd["bot_token"])
            baseurl = str(sd.get("baseurl") or base).rstrip("/")
            break
        print(f"  状态：{status or '等待扫码'}…")
    if not bot_token:
        raise SystemExit("✗ 扫码超时或未确认；重试：uv run python scripts/configure_weixin.py")

    print("✓ 扫码登录成功")

    env = _read_env(args.env_file)
    account_id = _prompt("WEIXIN_HOME_CHANNEL（默认推送/自测的 1v1 对端 user_id，可留空后续填）",
                         env.get("WEIXIN_HOME_CHANNEL", ""), allow_empty=True)
    updates = {
        "WEIXIN_TOKEN": bot_token,
        "WEIXIN_BASE_URL": baseurl,
        "WEIXIN_ACCOUNT_ID": env.get("WEIXIN_ACCOUNT_ID", "") or "ilink-bot",
        "WEIXIN_HOME_CHANNEL": account_id,
        "WEIXIN_GROUP_POLICY": env.get("WEIXIN_GROUP_POLICY") or "disabled",
    }
    _write_env(args.env_file, env, updates)
    print(f"✓ 已写入 {args.env_file}")
    print("\n下一步：")
    print("- 把 weixin 加入启用通道：CHANNELS_ENABLED=feishu,weixin（按需）")
    print("- 先在微信里给机器人私聊发一条消息，建立 context_token（定时推送依赖它，best-effort）")
    print("- 启动 gateway：bash scripts/start_gateway.sh")


def _prompt(name: str, default: str, *, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{name}{suffix}: ").strip() or default
    if not value and not allow_empty:
        print(f"✗ {name} 不能为空", file=sys.stderr)
        sys.exit(2)
    return value


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

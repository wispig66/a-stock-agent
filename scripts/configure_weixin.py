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
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for scan confirm")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")

    # 1. request login QR
    r = requests.get(f"{base}/ilink/bot/get_bot_qrcode", params={"bot_type": 3},
                     headers=_headers(), timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("ret") not in (0, None):
        raise SystemExit(f"✗ 获取二维码失败 ret={data.get('ret')}：{data}")
    qr_id = data.get("qrcode")
    content = data.get("qrcode_img_content") or ""
    if not qr_id:
        raise SystemExit(f"✗ 获取二维码失败：{data}")
    _render_login_qr(content, args.qr_out)
    print(f"  qrcode={qr_id}")

    # 2. poll scan status. 该接口长轮询：服务端最长约 35s 才返回，读超时必须 > 35s，
    #    否则会在「confirmed」状态返回前被切断（实测 30s 太短，扫了也取不到 token）。
    #    确认后返回顶层 {status:"confirmed", bot_token, baseurl}（iLink 官方协议）。
    deadline = time.time() + args.timeout
    bot_token = ""
    baseurl = base
    while time.time() < deadline:
        try:
            sr = requests.get(f"{base}/ilink/bot/get_qrcode_status",
                              params={"qrcode": qr_id}, headers=_headers(), timeout=60)
            sr.raise_for_status()
            sd = sr.json()
        except requests.RequestException as e:
            print(f"  轮询中…（{type(e).__name__}）")
            continue
        if sd.get("bot_token"):
            bot_token = str(sd["bot_token"])
            baseurl = str(sd.get("baseurl") or base).rstrip("/")
            break
        print(f"  状态：{sd.get('status') or '等待扫码'}…")
        time.sleep(1)
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


def _render_login_qr(content: str, png_out: Path) -> None:
    """``qrcode_img_content`` 实测是要编码进二维码的 URL（如 liteapp 短链）。

    优先在终端渲染 ASCII 二维码直接扫；若 pillow 可用顺带存一份 PNG。
    兜底：万一某些环境返回 base64 PNG，则按图片落盘。
    """
    if content.startswith("http"):
        try:
            import qrcode  # type: ignore
        except ImportError:
            print("⚠️ 渲染二维码需要 qrcode 库：uv add qrcode")
            print(f"  或手动把这个链接转成二维码用微信扫：{content}")
            return
        qr = qrcode.QRCode(border=2, box_size=10)
        qr.add_data(content)
        qr.make(fit=True)
        # 优先存 PNG 用预览扫（终端 ASCII 常被截断扫不全）。
        saved_png = False
        try:  # 需 pillow
            png_out.parent.mkdir(parents=True, exist_ok=True)
            qr.make_image().save(png_out)
            saved_png = True
        except Exception:
            pass
        if saved_png:
            print(f"\n✓ 登录二维码已存为图片：{png_out}")
            print(f"  打开后用手机微信扫码并确认登录：  open {png_out}")
            if sys.platform == "darwin":  # 顺手自动打开预览（best-effort）
                try:
                    import subprocess
                    subprocess.run(["open", str(png_out)], check=False)
                except Exception:
                    pass
        print("\n（或扫描下面的终端二维码，终端较小可能显示不全）：\n")
        qr.print_ascii(invert=True)
        return
    # 兜底：base64 PNG
    png_out.parent.mkdir(parents=True, exist_ok=True)
    try:
        png_out.write_bytes(base64.b64decode(content))
        print(f"✓ 登录二维码已保存：{png_out}，用微信扫码确认登录…")
    except Exception:
        print("⚠️ 二维码内容无法识别（既非 URL 也非 base64 图片）")


def _prompt(name: str, default: str, *, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{name}{suffix}: ").strip() or default
    except EOFError:  # 非交互运行（无 stdin）：用默认值
        value = default
        print(f"{name}{suffix}: (无 stdin，用默认值 {default!r})")
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

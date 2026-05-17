#!/usr/bin/env python3
"""L7 stock-weekly 周日 21:00 launchd 入口。

行为：
  1. 计算当周 label
  2. data/weekly_review/<label>.md 已存在 → 跳过（除非 --force）
  3. 否则 headless `claude -p` 触发 stock-weekly skill，超时 600s
  4. 全程日志到 logs/weekly_loop.log
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 600


def _project_root() -> Path:
    """Use cwd if it contains data/ (tests do chdir(tmp_path)); else default."""
    if (Path.cwd() / "data").exists() or (Path.cwd() / "data" / "weekly_review").exists():
        return Path.cwd()
    return DEFAULT_ROOT


def _log(msg: str) -> None:
    root = _project_root()
    log_dir = root / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "weekly_loop.log"
    ts = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    line = f"[{ts}] {msg}\n"
    log_path.open("a").write(line)
    print(line, end="", file=sys.stderr)


def _current_week_label(today: date) -> str:
    """当周 ISO label。锚定到本 ISO 周的周五。"""
    weekday = today.weekday()  # Mon=0..Sun=6
    if weekday == 6:  # Sunday
        friday = today - timedelta(days=2)
    elif weekday >= 4:  # Fri or Sat
        friday = today - timedelta(days=weekday - 4)
    else:  # Mon-Thu: 上周五
        friday = today - timedelta(days=weekday + 3)
    iso_year, iso_week, _ = friday.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _invoke_claude(timeout: int = TIMEOUT_SECONDS) -> int:
    """headless 触发 stock-weekly skill。stdin 给自然语言 prompt。"""
    root = _project_root()
    cmd = ["claude", "-p", "--permission-mode", "bypassPermissions"]
    prompt = "请运行 /stock-weekly：跑当周周复盘，输出长文 + TG 推送。"
    _log(f"invoking: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, input=prompt, text=True, timeout=timeout,
            capture_output=True, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        _log(f"TIMEOUT after {timeout}s")
        return 124
    _log(f"claude exit={result.returncode}")
    if result.stdout:
        _log("STDOUT (last 500 chars):\n" + result.stdout[-500:])
    if result.stderr:
        _log("STDERR (last 500 chars):\n" + result.stderr[-500:])
    return result.returncode


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="即使当周长文已存在也强制重跑")
    args = ap.parse_args(argv)

    root = _project_root()
    today = date.today()
    label = _current_week_label(today)
    out_path = root / "data" / "weekly_review" / f"{label}.md"

    if out_path.exists() and not args.force:
        _log(f"SKIP: {out_path} already exists; use --force to overwrite")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _log(f"START weekly loop for {label}")
    rc = _invoke_claude()
    _log(f"DONE rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

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
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 600

sys.path.insert(0, str(DEFAULT_ROOT / "code"))
from logger import get_logger, new_req_id, set_req_id, run_subprocess  # noqa: E402

log = get_logger("weekly_loop")


def _project_root() -> Path:
    """Use cwd if it contains data/ (tests do chdir(tmp_path)); else default."""
    if (Path.cwd() / "data").exists() or (Path.cwd() / "data" / "weekly_review").exists():
        return Path.cwd()
    return DEFAULT_ROOT


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
    try:
        result = run_subprocess(cmd, name="weekly_skill", timeout=timeout,
                                input_text=prompt, cwd=root)
    except subprocess.TimeoutExpired:
        return 124
    return result.returncode


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="即使当周长文已存在也强制重跑")
    args = ap.parse_args(argv)

    set_req_id(new_req_id())
    root = _project_root()
    today = date.today()
    label = _current_week_label(today)
    out_path = root / "data" / "weekly_review" / f"{label}.md"

    if out_path.exists() and not args.force:
        log.info("SKIP: %s 已存在；--force 可覆盖", out_path)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("START weekly loop for %s", label)
    rc = _invoke_claude()
    if rc == 0:
        log.info("DONE rc=0")
    else:
        log.error("DONE rc=%d", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())

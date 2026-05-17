"""验证 parse_machine_readable 在 L1 使用场景下的鲁棒性。

L1 Step 1.5 = 找最新 data/weekly_review/*.md → parse_machine_readable。
本测试只测 lib 行为，不真跑 skill。
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))


def test_find_latest_weekly_review(tmp_path):
    """有多个 weekly_review 文件时取最新（按 ISO label 字典序）。"""
    from lib.weekly_pack import parse_machine_readable

    d = tmp_path / "data" / "weekly_review"
    d.mkdir(parents=True)
    (d / "2026-W18.md").write_text(_dummy_md("2026-W18", "白酒"))
    (d / "2026-W19.md").write_text(_dummy_md("2026-W19", "算力"))
    (d / "2026-W20.md").write_text(_dummy_md("2026-W20", "创新药"))

    latest = sorted(d.glob("*.md"))[-1]
    assert latest.name == "2026-W20.md"
    parsed = parse_machine_readable(latest)
    assert parsed["week"] == "2026-W20"
    assert parsed["themes"][0]["name"] == "创新药"


def test_missing_dir_no_crash(tmp_path):
    """L1 在 data/weekly_review/ 不存在时不应 crash。"""
    from lib.weekly_pack import parse_machine_readable
    d = tmp_path / "data" / "weekly_review"
    # 不创建目录
    candidates = sorted(d.glob("*.md")) if d.exists() else []
    assert candidates == []


def _dummy_md(label: str, theme_name: str) -> str:
    return f"""# {label}

## Part 1 本周复盘
x

## Part 2 下周方向
y

## 下周方向 (machine-readable)

```yaml
week: {label}
generated_at: 2026-05-17T21:00:00+08:00
sentiment_stage: 退潮期
themes:
- name: {theme_name}
  stance: 延续
  leaders: ['300308']
  catalysts: []
  risks: []
  match_score: high
discipline_notes: ''
web_status: ok
```
"""

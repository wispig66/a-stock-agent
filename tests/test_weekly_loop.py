"""weekly_loop.py 去重 + --force 行为测试。"""
from __future__ import annotations
import subprocess
import sys
from datetime import date
from unittest.mock import patch

import pytest
from stock_codex.apps import weekly_loop


def test_skip_when_file_exists(tmp_path, monkeypatch):
    """data/weekly_review/<label>.md 已存在 → 跳过，不调 Codex。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "weekly_review").mkdir(parents=True)
    label = weekly_loop._current_week_label(date.today())
    (tmp_path / "data" / "weekly_review" / f"{label}.md").write_text("existing")

    with patch.object(weekly_loop, "_invoke_codex") as mock_invoke:
        rc = weekly_loop.main([])

    assert rc == 0
    mock_invoke.assert_not_called()


def test_force_overrides_skip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "weekly_review").mkdir(parents=True)
    label = weekly_loop._current_week_label(date.today())
    (tmp_path / "data" / "weekly_review" / f"{label}.md").write_text("existing")

    with patch.object(weekly_loop, "_invoke_codex", return_value=0) as mock_invoke:
        rc = weekly_loop.main(["--force"])

    assert rc == 0
    mock_invoke.assert_called_once()

"""风控模块单元测试。"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from lib import risk  # noqa: E402


def test_load_risk_config_missing(tmp_path, monkeypatch, capsys):
    """配置缺失时返回默认值并 stderr warning。"""
    monkeypatch.setattr(risk, "CONFIG_FILE", tmp_path / "nope.yaml")
    cfg = risk.load_risk_config()
    assert cfg["total_capital"] == 500000
    assert cfg["max_total_exposure_pct"] == 70
    assert cfg["max_single_position_pct"] == 30
    err = capsys.readouterr().err
    assert "risk_config.yaml" in err


def test_load_risk_config_present(tmp_path, monkeypatch):
    """配置存在时按文件值返回。"""
    f = tmp_path / "risk_config.yaml"
    f.write_text(
        "total_capital: 1000000\n"
        "max_total_exposure_pct: 60\n"
        "max_single_position_pct: 25\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(risk, "CONFIG_FILE", f)
    cfg = risk.load_risk_config()
    assert cfg["total_capital"] == 1000000
    assert cfg["max_total_exposure_pct"] == 60
    assert cfg["max_single_position_pct"] == 25

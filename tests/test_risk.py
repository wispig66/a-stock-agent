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


def _mk_holding(code, name, cost, shares):
    """构造测试用 Holding-like dict（兼容 compute_exposure 输入）。"""
    return {"code": code, "name": name, "cost": cost, "shares": shares}


def test_compute_exposure_empty():
    """空持仓 → 0 exposure。"""
    result = risk.compute_exposure([], total_capital=500000, price_fn=lambda c: None)
    assert result["total_value"] == 0
    assert result["exposure_pct"] == 0
    assert result["position_count"] == 0


def test_compute_exposure_with_realtime_price():
    """实时价可取时按实时价算。"""
    holdings = [
        _mk_holding("601991", "大唐发电", cost=6.77, shares=1000),
        _mk_holding("600519", "贵州茅台", cost=1500, shares=100),
    ]
    prices = {"601991": 7.0, "600519": 1600.0}
    result = risk.compute_exposure(
        holdings, total_capital=500000, price_fn=lambda c: prices.get(c)
    )
    assert result["total_value"] == pytest.approx(167000.0)
    assert result["exposure_pct"] == pytest.approx(33.4, abs=0.01)
    assert result["position_count"] == 2


def test_compute_exposure_price_fallback_to_cost():
    """实时价取不到时兜底用 cost。"""
    holdings = [_mk_holding("601991", "大唐发电", cost=6.77, shares=1000)]
    result = risk.compute_exposure(
        holdings, total_capital=500000, price_fn=lambda c: None
    )
    assert result["total_value"] == pytest.approx(6770.0)
    assert result["exposure_pct"] == pytest.approx(1.354, abs=0.01)

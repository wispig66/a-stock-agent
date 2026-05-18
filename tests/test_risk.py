"""风控模块单元测试。"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from lib import risk  # noqa: E402


def test_code_with_market_prefix():
    assert risk._code_with_market_prefix("600000") == "sh600000"
    assert risk._code_with_market_prefix("000001") == "sz000001"
    assert risk._code_with_market_prefix("300033") == "sz300033"
    assert risk._code_with_market_prefix("688409") == "sh688409"
    assert risk._code_with_market_prefix("832145") == "bj832145"


def test_fetch_prices_for_codes_all_sources_fail(monkeypatch):
    """sina + tencent + akshare 全失败 → source='none'，不要按 cost 兜底。"""
    def boom(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(risk, "_fetch_prices_sina", boom)
    monkeypatch.setattr(risk, "_fetch_prices_tencent", boom)
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **kw):
        if name == "akshare":
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    prices, src = risk.fetch_prices_for_codes(["600000"])
    assert src == "none"
    assert prices == {}


def test_fetch_prices_for_codes_sina_success(monkeypatch):
    """sina 拿到数据 → 直接返回，不再走 tencent/akshare。"""
    def sina_ok(codes):
        return {c: 10.0 for c in codes}
    def tencent_panic(*a, **kw):
        raise AssertionError("tencent 不应被调用")
    monkeypatch.setattr(risk, "_fetch_prices_sina", sina_ok)
    monkeypatch.setattr(risk, "_fetch_prices_tencent", tencent_panic)
    prices, src = risk.fetch_prices_for_codes(["600000", "000001"])
    assert src == "sina"
    assert prices == {"600000": 10.0, "000001": 10.0}


def test_fetch_prices_for_codes_sina_fail_tencent_ok(monkeypatch):
    monkeypatch.setattr(risk, "_fetch_prices_sina",
                        lambda c: (_ for _ in ()).throw(RuntimeError("sina down")))
    monkeypatch.setattr(risk, "_fetch_prices_tencent",
                        lambda c: {x: 20.0 for x in c})
    prices, src = risk.fetch_prices_for_codes(["600000"])
    assert src == "tencent"
    assert prices == {"600000": 20.0}


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


def test_preflight_check_under_limit():
    """未超额：无横幅，剩余可用 = 上限 - 当前。"""
    exposure = {"total_value": 250000.0, "exposure_pct": 50.0, "position_count": 3}
    cfg = {"total_capital": 500000, "max_total_exposure_pct": 70, "max_single_position_pct": 30}
    r = risk.preflight_check(exposure, cfg)
    assert r["ok"] is True
    assert r["banner"] is None
    assert r["available_pct"] == 20.0


def test_preflight_check_over_limit():
    """超额：横幅出现，available 取 0。"""
    exposure = {"total_value": 360000.0, "exposure_pct": 72.0, "position_count": 5}
    cfg = {"total_capital": 500000, "max_total_exposure_pct": 70, "max_single_position_pct": 30}
    r = risk.preflight_check(exposure, cfg)
    assert r["ok"] is False
    assert r["banner"] is not None
    assert "72" in r["banner"] and "70" in r["banner"]
    assert r["available_pct"] == 0.0


def test_preflight_check_at_exact_limit():
    """正好等于上限：算未超额，available=0。"""
    exposure = {"total_value": 350000.0, "exposure_pct": 70.0, "position_count": 4}
    cfg = {"total_capital": 500000, "max_total_exposure_pct": 70, "max_single_position_pct": 30}
    r = risk.preflight_check(exposure, cfg)
    assert r["ok"] is True
    assert r["banner"] is None
    assert r["available_pct"] == 0.0


def test_make_price_fn_from_df():
    """price_fn 工厂：从 DataFrame 构造 code -> price 闭包。"""
    import pandas as pd
    df = pd.DataFrame(
        [
            {"代码": "601991", "最新价": 7.05},
            {"代码": "600519", "最新价": 1601.23},
        ]
    )
    fn = risk.make_price_fn_from_df(df)
    assert fn("601991") == pytest.approx(7.05)
    assert fn("600519") == pytest.approx(1601.23)
    assert fn("999999") is None


def test_make_price_fn_handles_nan():
    """NaN 最新价（停牌）返回 None。"""
    import pandas as pd
    df = pd.DataFrame([{"代码": "601991", "最新价": float("nan")}])
    fn = risk.make_price_fn_from_df(df)
    assert fn("601991") is None

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
FETCH_POSTMARKET = ROOT / ".agents" / "skills" / "stock-postmarket" / "scripts" / "fetch_postmarket.py"


def load_module():
    spec = importlib.util.spec_from_file_location("postmarket_fetch", FETCH_POSTMARKET)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixedDatetime:
    @classmethod
    def now(cls):
        return datetime(2026, 5, 21, 15, 35, 0)


def test_structured_table_retries_then_caches_success(tmp_path, monkeypatch) -> None:
    fp = load_module()
    monkeypatch.setattr(fp, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fp, "datetime", FixedDatetime)
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("temporary dns failure")
        return pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "涨跌幅": 10.0}])

    df = fp._fetch_structured_table(
        label="涨停池",
        cache_name="zt",
        date="20260521",
        fetcher=fetcher,
        attempts=3,
        sleep_seconds=0,
    )

    assert calls == 3
    assert df.to_dict("records") == [{"代码": "600000", "名称": "浦发银行", "涨跌幅": 10.0}]
    assert (tmp_path / "20260521_zt.json").exists()


def test_spot_snapshot_caches_pruned_full_market_snapshot(tmp_path, monkeypatch) -> None:
    fp = load_module()
    monkeypatch.setattr(fp, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fp, "datetime", FixedDatetime)
    monkeypatch.setattr(
        fp.ak,
        "stock_zh_a_spot_em",
        lambda: pd.DataFrame([
            {"代码": "600000", "名称": "浦发银行", "最新价": 10.4, "涨跌幅": 3.0, "最高": 10.5, "最低": 9.9, "无关列": "x"},
        ]),
    )

    df = fp.fetch_spot_snapshot("20260521")

    assert df.to_dict("records") == [
        {"代码": "600000", "名称": "浦发银行", "最新价": 10.4, "涨跌幅": 3.0, "最高": 10.5, "最低": 9.9},
    ]
    assert (tmp_path / "20260521_spot.json").exists()


def test_structured_table_uses_same_day_cache_after_failures(tmp_path, monkeypatch) -> None:
    fp = load_module()
    monkeypatch.setattr(fp, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fp, "datetime", FixedDatetime)
    original = pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "涨跌幅": 10.0}])
    fp._write_df_cache("20260521", "zt", original)

    df = fp._fetch_structured_table(
        label="涨停池",
        cache_name="zt",
        date="20260521",
        fetcher=lambda: (_ for _ in ()).throw(OSError("dns down")),
        attempts=2,
        sleep_seconds=0,
    )

    assert df.to_dict("records") == [{"代码": "600000", "名称": "浦发银行", "涨跌幅": 10.0}]
    assert df.attrs["snapshot_source"] == "cache"
    assert df.attrs["snapshot_at"] == "2026-05-21T15:35:00"


def test_all_empty_structural_sources_fail_closed() -> None:
    fp = load_module()

    with pytest.raises(fp.DataUnavailable, match="拒绝写入 0 数据 fact pack"):
        fp.assert_structural_data_available(
            zt=pd.DataFrame(),
            zd=pd.DataFrame(),
            zb=pd.DataFrame(),
            qs=pd.DataFrame(),
        )


def test_allowed_marks_cached_postmarket_sources() -> None:
    fp = load_module()
    zt = pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "连板数": 1, "涨跌幅": 10.0}])
    zt.attrs["snapshot_source"] = "cache"
    zt.attrs["snapshot_at"] = "2026-05-21T15:35:00"
    sent = {
        "limit_up_count": 1,
        "limit_down_count": 0,
        "max_consec": 1,
        "phase": "启动",
    }

    allowed = fp.build_allowed(
        iso="2026-05-21",
        sent=sent,
        zt=zt,
        zd=pd.DataFrame(),
        zb=pd.DataFrame(),
        hot=pd.DataFrame(),
        lhb={"stocks": []},
        ladder={},
        global_markets={},
    )

    assert allowed["summary"]["limit_up"] == 1
    assert allowed["summary"]["limit_up_snapshot_source"] == "cache"
    assert allowed["summary"]["limit_up_snapshot_at"] == "2026-05-21T15:35:00"

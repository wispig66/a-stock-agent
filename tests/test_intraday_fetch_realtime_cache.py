from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FETCH_REALTIME = ROOT / ".agents" / "skills" / "stock-intraday" / "scripts" / "fetch_realtime.py"


def load_module():
    spec = importlib.util.spec_from_file_location("intraday_fetch_realtime", FETCH_REALTIME)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixedDatetime:
    @classmethod
    def now(cls):
        return datetime(2026, 5, 21, 14, 30, 0)


def test_structured_table_retries_then_caches_success(tmp_path, monkeypatch) -> None:
    fr = load_module()
    monkeypatch.setattr(fr, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fr, "datetime", FixedDatetime)
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("temporary dns failure")
        return pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "涨跌幅": 10.0}])

    df = fr._fetch_structured_table(
        label="涨停池",
        cache_name="zt_pool",
        fetcher=fetcher,
        attempts=3,
        sleep_seconds=0,
    )

    assert calls == 3
    assert df.to_dict("records") == [{"代码": "600000", "名称": "浦发银行", "涨跌幅": 10.0}]
    assert (tmp_path / "20260521_zt_pool.json").exists()


def test_structured_table_uses_same_day_cache_after_failures(tmp_path, monkeypatch) -> None:
    fr = load_module()
    monkeypatch.setattr(fr, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fr, "datetime", FixedDatetime)
    original = pd.DataFrame([{"概念名称": "机器人", "涨跌幅": 8.5}])
    fr._write_df_cache("concept_hot", original)

    df = fr._fetch_structured_table(
        label="概念热度",
        cache_name="concept_hot",
        fetcher=lambda: (_ for _ in ()).throw(OSError("dns down")),
        attempts=2,
        sleep_seconds=0,
    )

    assert df.to_dict("records") == [{"概念名称": "机器人", "涨跌幅": 8.5}]
    assert df.attrs["snapshot_source"] == "cache"
    assert df.attrs["snapshot_at"] == "2026-05-21T14:30:00"
    assert fr._cache_note(df) == "（使用缓存快照：2026-05-21T14:30:00，实时源失败）"


def test_allowed_marks_cached_structural_sources(tmp_path, monkeypatch) -> None:
    fr = load_module()
    monkeypatch.setattr(fr, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fr, "datetime", FixedDatetime)
    zt = pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "连板数": 1, "涨跌幅": 10.0}])
    zt.attrs["snapshot_source"] = "cache"
    zt.attrs["snapshot_at"] = "2026-05-21T14:30:00"

    allowed = fr._build_allowed(
        watchlist=[],
        holdings=[],
        spot=pd.DataFrame(),
        zt=zt,
        zb=pd.DataFrame(),
        cc=pd.DataFrame(),
        now=datetime(2026, 5, 21, 14, 31, 0),
        label="尾盘快照（14:30）",
    )

    assert allowed["summary"]["limit_up"] == 1
    assert allowed["summary"]["limit_up_snapshot_source"] == "cache"
    assert allowed["summary"]["limit_up_snapshot_at"] == "2026-05-21T14:30:00"

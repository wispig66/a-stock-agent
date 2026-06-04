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


def test_allowed_collects_concepts_from_eastmoney_shape() -> None:
    fr = load_module()
    cc = pd.DataFrame([
        {"板块名称": "昨日连板", "涨跌幅": 3.71},
        {"板块名称": "蓝宝石", "涨跌幅": 3.24},
    ])

    allowed = fr._build_allowed(
        watchlist=[],
        holdings=[],
        spot=pd.DataFrame(),
        zt=pd.DataFrame(),
        zb=pd.DataFrame(),
        cc=cc,
        now=datetime(2026, 5, 21, 14, 31, 0),
        label="尾盘快照（14:30）",
    )

    assert allowed["concepts"] == ["昨日连板", "蓝宝石"]


def test_allowed_v2_includes_market_narrative_fields() -> None:
    fr = load_module()

    allowed = fr._build_allowed(
        watchlist=[],
        holdings=[],
        spot=pd.DataFrame(),
        zt=pd.DataFrame(),
        zb=pd.DataFrame(),
        cc=pd.DataFrame(),
        now=datetime(2026, 5, 21, 14, 31, 0),
        label="尾盘快照（14:30）",
    )

    assert allowed["schema_version"] == "2"
    assert allowed["summary"]["market_snapshot_stale"] is True
    assert allowed["market_breadth"] == {}
    assert allowed["indices"] == {}
    assert allowed["turnover"] == {}
    assert allowed["theme_strength"] == {}
    assert allowed["overseas"] == {}
    assert allowed["anchors"] == {}
    assert allowed["pool_summary"] == {}


def test_allowed_v2_drops_stale_shared_market_narrative_fields() -> None:
    fr = load_module()
    market_snapshot = {
        "snapshot_ts": "2026-05-21T14:00:00",
        "is_stale": True,
        "news": [{"title": "过期新闻"}],
        "breadth": {"up": 3000, "down": 2000},
        "theme_strength": {"CPO光模块": {"pct": 5.0}},
        "overseas": {"NVDA": {"pct": 3.0}},
        "anchors": {"300308": {"name": "中际旭创"}},
    }

    allowed = fr._build_allowed(
        watchlist=[],
        holdings=[],
        spot=pd.DataFrame(),
        zt=pd.DataFrame(),
        zb=pd.DataFrame(),
        cc=pd.DataFrame(),
        now=datetime(2026, 5, 21, 14, 31, 0),
        label="尾盘快照（14:30）",
        market_snapshot=market_snapshot,
    )

    assert allowed["summary"]["market_snapshot_stale"] is True
    assert allowed["news"] == []
    assert allowed["market_breadth"] == {}
    assert allowed["theme_strength"] == {}
    assert allowed["overseas"] == {}
    assert allowed["anchors"] == {}


def test_fetch_concept_hot_falls_back_when_ths_has_only_name_shape(monkeypatch) -> None:
    fr = load_module()
    ths_df = pd.DataFrame([
        {"name": "液冷服务器", "code": "309999"},
        {"name": "煤炭", "code": "300123"},
    ])
    em_df = pd.DataFrame([
        {"板块名称": "光纤概念", "涨跌幅": 3.86},
        {"板块名称": "培育钻石", "涨跌幅": 4.53},
    ])

    monkeypatch.setattr(fr.ak, "stock_board_concept_name_ths", lambda: ths_df)
    monkeypatch.setattr(fr.ak, "stock_board_concept_name_em", lambda: em_df)

    cc = fr.fetch_concept_hot()

    assert cc.to_dict("records") == [
        {"板块名称": "培育钻石", "涨跌幅": 4.53},
        {"板块名称": "光纤概念", "涨跌幅": 3.86},
    ]


def test_fetch_concept_hot_rejects_name_only_cache(tmp_path, monkeypatch) -> None:
    fr = load_module()
    monkeypatch.setattr(fr, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fr, "datetime", FixedDatetime)
    fr._write_df_cache("concept_hot", pd.DataFrame([{"name": "液冷服务器", "code": "309999"}]))
    monkeypatch.setattr(
        fr.ak,
        "stock_board_concept_name_ths",
        lambda: (_ for _ in ()).throw(OSError("ths down")),
    )

    cc = fr.fetch_concept_hot()

    assert cc.empty

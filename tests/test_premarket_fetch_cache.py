from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
FETCH_PREMARKET = ROOT / ".agents" / "skills" / "stock-premarket" / "scripts" / "fetch_data.py"


def load_module():
    spec = importlib.util.spec_from_file_location("premarket_fetch", FETCH_PREMARKET)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixedDatetime:
    @classmethod
    def now(cls):
        return datetime(2026, 5, 22, 8, 0, 0)


def write_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cached_at": "2026-05-21T15:44:41",
        "data": json.loads(df.to_json(orient="split", force_ascii=False, date_format="iso")),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_structured_table_retries_then_caches_success(tmp_path, monkeypatch) -> None:
    fp = load_module()
    monkeypatch.setattr(fp, "PREMARKET_CACHE_DIR", tmp_path / "premarket_cache")
    monkeypatch.setattr(fp, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(fp, "INTRADAY_CACHE_DIR", tmp_path / "intraday_cache")
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
    assert (tmp_path / "premarket_cache" / "20260521_zt.json").exists()


def test_uses_postmarket_cache_after_live_failures(tmp_path, monkeypatch) -> None:
    fp = load_module()
    monkeypatch.setattr(fp, "PREMARKET_CACHE_DIR", tmp_path / "premarket_cache")
    monkeypatch.setattr(fp, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(fp, "INTRADAY_CACHE_DIR", tmp_path / "intraday_cache")
    original = pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "连板数": 1, "涨跌幅": 10.0}])
    write_cache(tmp_path / "postmarket_cache" / "20260521_zt.json", original)

    df = fp._fetch_structured_table(
        label="涨停池",
        cache_name="zt",
        date="20260521",
        fetcher=lambda: (_ for _ in ()).throw(OSError("dns down")),
        attempts=2,
        sleep_seconds=0,
    )

    assert df.to_dict("records") == [{"代码": "600000", "名称": "浦发银行", "连板数": 1, "涨跌幅": 10.0}]
    assert df.attrs["snapshot_source"] == "cache"
    assert df.attrs["snapshot_cache"] == "postmarket_cache"
    assert df.attrs["snapshot_at"] == "2026-05-21T15:44:41"


def test_allowed_marks_cached_premarket_sources() -> None:
    fp = load_module()
    zt = pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "连板数": 1, "涨跌幅": 10.0}])
    zt.attrs["snapshot_source"] = "cache"
    zt.attrs["snapshot_at"] = "2026-05-21T15:44:41"
    zt.attrs["snapshot_cache"] = "postmarket_cache"

    allowed = fp.build_allowed(
        date="20260521",
        zt=zt,
        zb=pd.DataFrame(),
        lhb=pd.DataFrame(),
        hot=pd.DataFrame(),
        news=pd.DataFrame(),
    )

    assert allowed["summary"]["limit_up"] == 1
    assert allowed["summary"]["limit_up_snapshot_source"] == "cache"
    assert allowed["summary"]["limit_up_snapshot_at"] == "2026-05-21T15:44:41"
    assert allowed["summary"]["limit_up_snapshot_cache"] == "postmarket_cache"


def test_allowed_includes_existing_holdings(monkeypatch) -> None:
    fp = load_module()
    monkeypatch.setattr(
        fp,
        "read_holdings",
        lambda: [type("Holding", (), {"code": "002608", "name": "江苏国信"})()],
    )

    allowed = fp.build_allowed(
        date="20260521",
        zt=pd.DataFrame(),
        zb=pd.DataFrame(),
        lhb=pd.DataFrame(),
        hot=pd.DataFrame(),
        news=pd.DataFrame(),
    )

    assert allowed["codes"]["002608"] == "江苏国信"


def test_last_trade_day_uses_latest_cached_day_after_live_probe_failures(tmp_path, monkeypatch) -> None:
    fp = load_module()
    monkeypatch.setattr(fp, "PREMARKET_CACHE_DIR", tmp_path / "premarket_cache")
    monkeypatch.setattr(fp, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(fp, "INTRADAY_CACHE_DIR", tmp_path / "intraday_cache")
    monkeypatch.setattr(fp, "datetime", FixedDatetime)
    write_cache(
        tmp_path / "postmarket_cache" / "20260521_zt.json",
        pd.DataFrame([{"代码": "600000", "名称": "浦发银行"}]),
    )
    monkeypatch.setattr(
        fp.ak,
        "stock_zt_pool_em",
        lambda date: (_ for _ in ()).throw(OSError("dns down")),
    )

    assert fp.last_trade_day() == "20260521"


def test_render_core_pack_fails_closed_without_limit_up_data(monkeypatch) -> None:
    fp = load_module()
    monkeypatch.setattr(fp, "fetch_zt_pool", lambda date: pd.DataFrame())

    with pytest.raises(fp.DataUnavailable, match="涨停池无数据"):
        fp.render_core_pack("20260521")


def test_overnight_news_continues_after_cls_timeout(monkeypatch) -> None:
    fp = load_module()

    def fetch_cls():
        raise fp.SourceTimeout("source call exceeded 20s")

    def fetch_em():
        return pd.DataFrame([{
            "发布时间": "2026-05-21 16:30:00",
            "标题": "CPO概念再度走强",
            "链接": "https://example.test/news",
        }])

    monkeypatch.setattr(fp, "_call_with_timeout", lambda seconds, fn: fn())
    monkeypatch.setattr(fp.ak, "stock_info_global_cls", lambda symbol: fetch_cls())
    monkeypatch.setattr(fp.ak, "stock_info_global_em", fetch_em)
    monkeypatch.setattr(fp.ak, "stock_info_global_sina", lambda: pd.DataFrame())

    df = fp.fetch_overnight_news("20260521")

    assert df.to_dict("records") == [{
        "发布时间": datetime(2026, 5, 21, 16, 30),
        "来源": "EM",
        "标题": "CPO概念再度走强",
        "URL": "https://example.test/news",
    }]


def test_overnight_news_returns_empty_when_all_sources_fail(monkeypatch) -> None:
    fp = load_module()

    monkeypatch.setattr(
        fp,
        "_call_with_timeout",
        lambda seconds, fn: (_ for _ in ()).throw(fp.SourceTimeout("source call exceeded 20s")),
    )

    df = fp.fetch_overnight_news("20260521")

    assert df.empty
    assert list(df.columns) == ["发布时间", "来源", "标题", "URL"]


def test_news_theme_tags_use_catalog_aliases_without_mapping_fiber_to_cpo() -> None:
    fp = load_module()
    news = pd.DataFrame([
        {"标题": "光纤需求增长"},
        {"标题": "AI 行业出现新催化"},
    ])

    tagged = fp.tag_news_themes(news)

    assert "光纤光缆" in tagged.iloc[0]["命中题材"]
    assert "CPO光模块" not in tagged.iloc[0]["命中题材"]
    assert tagged.iloc[1]["命中题材"] == "AI硬件"

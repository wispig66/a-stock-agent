from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from stock_codex.market.market_snapshot import MarketSnapshot
from stock_codex.market.theme_graph import ThemeGraph


def make_graph(tmp_path: Path) -> ThemeGraph:
    catalog = tmp_path / "concept_whitelist.yaml"
    catalog.write_text(
        """
AI硬件:
  aliases: [AI]
  members: []
CPO光模块:
  parent: AI硬件
  aliases: [CPO, 光模块]
  members:
    anchors: [300308]
光纤光缆:
  parent: AI硬件
  aliases: [光纤, 光缆]
  members:
    anchors: [600487]
""".lstrip(),
        encoding="utf-8",
    )
    return ThemeGraph(catalog, db_path=tmp_path / "daily.db")


def _a_spot() -> pd.DataFrame:
    return pd.DataFrame([
        {"代码": "sz300308", "名称": "中际旭创", "最新价": 100.0, "涨跌幅": 6.0, "成交额": 2_000_000_000},
        {"代码": "sh600487", "名称": "亨通光电", "最新价": 20.0, "涨跌幅": 3.0, "成交额": 800_000_000},
        {"代码": "sh600000", "名称": "浦发银行", "最新价": 10.0, "涨跌幅": -1.0, "成交额": 500_000_000},
    ])


def _indices() -> pd.DataFrame:
    return pd.DataFrame([
        {"名称": "上证指数", "涨跌幅": 0.2, "成交额": 1_000_000_000_000},
        {"名称": "创业板指", "涨跌幅": 1.1, "成交额": 400_000_000_000},
    ])


def _concept_flow() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "行业": "光纤概念",
            "行业-涨跌幅": 3.8,
            "净额": 120.0,
            "公司家数": 102,
            "领涨股": "亨通光电",
            "领涨股-涨跌幅": 9.0,
        },
    ])


def test_capture_builds_compact_snapshot_and_persists_it(tmp_path) -> None:
    graph = make_graph(tmp_path)
    db = tmp_path / "daily.db"
    service = MarketSnapshot(
        db,
        graph,
        a_spot_fetcher=_a_spot,
        index_fetcher=_indices,
        concept_flow_fetcher=_concept_flow,
        overseas_fetcher=lambda: {"NVDA": {"pct": 2.5, "price": 150.0, "themes": ["AI硬件"]}},
        news_fetcher=lambda: [{"title": "光纤需求增长", "time": "09:34:00"}],
    )

    snapshot = service.capture(datetime(2026, 6, 3, 9, 35))

    assert snapshot["is_new"] is True
    assert snapshot["is_stale"] is False
    assert snapshot["breadth"]["up"] == 2
    assert snapshot["breadth"]["down"] == 1
    assert snapshot["turnover"]["amount"] == 3_300_000_000
    assert snapshot["indices"]["创业板指"]["pct"] == 1.1
    assert snapshot["theme_strength"]["光纤光缆"]["pct"] == 3.8
    assert snapshot["theme_strength"]["光纤光缆"]["leader"] == "亨通光电"
    assert snapshot["anchors"]["600487"]["pct"] == 3.0
    assert snapshot["news"][0]["themes"][0] == "光纤光缆"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_snapshot").fetchone()[0] == 1


def test_capture_reuses_snapshot_inside_five_minutes(tmp_path) -> None:
    graph = make_graph(tmp_path)
    calls = 0

    def fetch_spot():
        nonlocal calls
        calls += 1
        return _a_spot()

    service = MarketSnapshot(
        tmp_path / "daily.db",
        graph,
        a_spot_fetcher=fetch_spot,
        index_fetcher=_indices,
        concept_flow_fetcher=_concept_flow,
        overseas_fetcher=lambda: {},
        news_fetcher=lambda: [],
    )
    service.capture(datetime(2026, 6, 3, 9, 35))

    snapshot = service.capture(datetime(2026, 6, 3, 9, 38))

    assert calls == 1
    assert snapshot["is_new"] is False


def test_failed_sources_reuse_cache_but_become_stale_after_ten_minutes(tmp_path) -> None:
    graph = make_graph(tmp_path)
    state = {"fail": False}

    def maybe(fetcher):
        def _inner():
            if state["fail"]:
                raise OSError("source down")
            return fetcher()
        return _inner

    service = MarketSnapshot(
        tmp_path / "daily.db",
        graph,
        a_spot_fetcher=maybe(_a_spot),
        index_fetcher=maybe(_indices),
        concept_flow_fetcher=maybe(_concept_flow),
        overseas_fetcher=maybe(lambda: {}),
        news_fetcher=maybe(lambda: []),
    )
    service.capture(datetime(2026, 6, 3, 9, 35))
    state["fail"] = True

    snapshot = service.capture(datetime(2026, 6, 3, 9, 46))

    assert snapshot["is_new"] is True
    assert snapshot["is_stale"] is True
    assert snapshot["source_status"]["a_spot"]["source"] == "cache"
    assert snapshot["source_status"]["a_spot"]["age_seconds"] == 660


def test_reused_snapshot_rechecks_cache_age_before_next_five_minute_fetch(tmp_path) -> None:
    graph = make_graph(tmp_path)
    state = {"fail": False}

    def maybe(fetcher):
        def _inner():
            if state["fail"]:
                raise OSError("source down")
            return fetcher()
        return _inner

    service = MarketSnapshot(
        tmp_path / "daily.db",
        graph,
        a_spot_fetcher=maybe(_a_spot),
        index_fetcher=maybe(_indices),
        concept_flow_fetcher=maybe(_concept_flow),
        overseas_fetcher=maybe(
            lambda: {"NVDA": {"pct": 3.0, "price": 150.0, "themes": ["AI硬件"]}}
        ),
        news_fetcher=maybe(lambda: [{"title": "光纤需求增长", "time": "09:34:00"}]),
    )
    service.capture(datetime(2026, 6, 3, 9, 35))
    state["fail"] = True
    service.capture(datetime(2026, 6, 3, 9, 44))

    snapshot = service.capture(datetime(2026, 6, 3, 9, 46))

    assert snapshot["is_new"] is False
    assert snapshot["is_stale"] is True
    assert snapshot["source_status"]["a_spot"]["age_seconds"] == 660
    assert snapshot["overseas"] == {}
    assert snapshot["news"] == []


def test_expired_news_and_overseas_cache_cannot_keep_catalyst_signal(tmp_path) -> None:
    graph = make_graph(tmp_path)
    state = {"fail_catalyst": False}

    def overseas():
        if state["fail_catalyst"]:
            raise OSError("overseas down")
        return {"NVDA": {"pct": 3.0, "price": 150.0, "themes": ["AI硬件"]}}

    def news():
        if state["fail_catalyst"]:
            raise OSError("news down")
        return [{"title": "光纤需求增长", "time": "09:34:00"}]

    service = MarketSnapshot(
        tmp_path / "daily.db",
        graph,
        a_spot_fetcher=_a_spot,
        index_fetcher=_indices,
        concept_flow_fetcher=_concept_flow,
        overseas_fetcher=overseas,
        news_fetcher=news,
    )
    service.capture(datetime(2026, 6, 3, 9, 35))
    state["fail_catalyst"] = True

    snapshot = service.capture(datetime(2026, 6, 3, 9, 46))

    assert snapshot["is_stale"] is False
    assert snapshot["overseas"] == {}
    assert snapshot["news"] == []


def test_nonempty_sources_without_pct_fields_cannot_feed_signal_snapshot(tmp_path) -> None:
    graph = make_graph(tmp_path)
    service = MarketSnapshot(
        tmp_path / "daily.db",
        graph,
        a_spot_fetcher=lambda: pd.DataFrame([
            {"代码": "sz300308", "名称": "中际旭创", "最新价": 100.0},
        ]),
        index_fetcher=_indices,
        concept_flow_fetcher=lambda: pd.DataFrame([
            {"行业": "光纤概念", "净额": 120.0, "公司家数": 102},
        ]),
        overseas_fetcher=lambda: {},
        news_fetcher=lambda: [],
    )

    snapshot = service.capture(datetime(2026, 6, 3, 9, 35))

    assert snapshot["is_stale"] is True
    assert snapshot["stocks"] == {}
    assert snapshot["concept_flow"] == []
    assert snapshot["theme_strength"] == {}


def test_expired_index_cache_is_removed_without_staling_live_signal_sources(tmp_path) -> None:
    graph = make_graph(tmp_path)
    state = {"fail_indices": False}

    def indices():
        if state["fail_indices"]:
            raise OSError("indices down")
        return _indices()

    service = MarketSnapshot(
        tmp_path / "daily.db",
        graph,
        a_spot_fetcher=_a_spot,
        index_fetcher=indices,
        concept_flow_fetcher=_concept_flow,
        overseas_fetcher=lambda: {},
        news_fetcher=lambda: [],
    )
    service.capture(datetime(2026, 6, 3, 9, 35))
    state["fail_indices"] = True

    snapshot = service.capture(datetime(2026, 6, 3, 9, 46))

    assert snapshot["is_stale"] is False
    assert snapshot["indices"] == {}

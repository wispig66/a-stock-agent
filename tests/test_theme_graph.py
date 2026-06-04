from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from stock_codex.market.theme_graph import PACKAGED_CATALOG_PATH, ThemeGraph


CATALOG = """
AI硬件:
  aliases: [AI, AI硬件]
  keywords: [AI硬件]
  external_symbols:
    NVDA: anchor
CPO光模块:
  parent: AI硬件
  aliases: [CPO, 光模块]
  keywords: [CPO, 光模块]
  members:
    anchors: [300308]
光纤光缆:
  parent: AI硬件
  aliases: [光纤, 光缆]
  keywords: [光纤, 光缆]
  members: []
铜缆高速连接:
  parent: AI硬件
  aliases: [铜缆, 高速连接]
  keywords: [铜缆, 高速连接]
  members: []
""".lstrip()


def make_graph(tmp_path: Path, *, with_db: bool = False) -> ThemeGraph:
    catalog = tmp_path / "concept_whitelist.yaml"
    catalog.write_text(CATALOG, encoding="utf-8")
    db = tmp_path / "daily.db"
    if with_db:
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE ths_hot_reason (date TEXT, code TEXT, reason TEXT)")
    return ThemeGraph(catalog, db_path=db)


def test_resolve_returns_multiple_labels_parent_and_primary(tmp_path) -> None:
    graph = make_graph(tmp_path)

    matches = graph.resolve(
        "300308",
        "中际旭创",
        "",
        "铜缆高速连接异动",
        datetime(2026, 6, 3, 10, 0),
    )

    by_theme = {m.theme: m for m in matches}
    assert {"CPO光模块", "铜缆高速连接", "AI硬件"} <= set(by_theme)
    assert by_theme["CPO光模块"].source == "catalog_member"
    assert by_theme["CPO光模块"].is_primary is True
    assert by_theme["CPO光模块"].parent == "AI硬件"


def test_generic_ai_only_hits_parent_theme(tmp_path) -> None:
    graph = make_graph(tmp_path)

    matches = graph.resolve("", "", "", "AI 行业出现新催化", date(2026, 6, 3))

    assert [m.theme for m in matches] == ["AI硬件"]


def test_fiber_news_does_not_map_to_cpo(tmp_path) -> None:
    graph = make_graph(tmp_path)

    matches = graph.resolve("", "", "", "光纤需求增长", date(2026, 6, 3))

    themes = {m.theme for m in matches}
    assert "光纤光缆" in themes
    assert "CPO光模块" not in themes


def test_reason_scores_use_only_recent_trading_dates_and_hide_same_day_intraday(tmp_path) -> None:
    graph = make_graph(tmp_path, with_db=True)
    with sqlite3.connect(graph.db_path) as conn:
        conn.executemany(
            "INSERT INTO ths_hot_reason(date, code, reason) VALUES (?, ?, ?)",
            [
                ("2026-06-03", "600000", "光纤"),
                ("2026-06-02", "600000", "CPO"),
                ("2026-06-01", "600000", "铜缆"),
                ("2026-05-30", "600000", "完全无关"),
                ("2026-05-29", "600000", "光纤"),
                ("2026-05-28", "600000", "光模块"),
            ],
        )

    intraday = graph.resolve("600000", "测试股", "", "", datetime(2026, 6, 3, 10, 0))
    by_theme = {m.theme: m for m in intraday}
    assert by_theme["CPO光模块"].confidence == 0.75
    assert by_theme["铜缆高速连接"].confidence == 0.55
    assert "光纤光缆" not in by_theme

    end_of_day = graph.resolve("600000", "测试股", "", "", date(2026, 6, 3))
    assert {m.theme: m for m in end_of_day}["光纤光缆"].confidence == 0.90


def test_unknown_sector_hint_becomes_non_candidate_temporary_theme(tmp_path) -> None:
    graph = make_graph(tmp_path)

    matches = graph.resolve("", "", "新型未知概念", "", date(2026, 6, 3))

    assert len(matches) == 1
    assert matches[0].theme == "新型未知概念"
    assert matches[0].temporary is True
    assert matches[0].candidate_allowed is False


def test_external_symbol_mapping_returns_catalog_theme(tmp_path) -> None:
    graph = make_graph(tmp_path)

    matches = graph.resolve_external("nvda")

    assert [m.theme for m in matches] == ["AI硬件"]
    assert matches[0].source == "external_symbol"


def test_missing_local_catalog_can_fall_back_to_packaged_default(tmp_path) -> None:
    graph = ThemeGraph(
        tmp_path / "missing-local-catalog.yaml",
        db_path=tmp_path / "daily.db",
        fallback_path=PACKAGED_CATALOG_PATH,
    )

    assert "AI硬件" in graph.themes
    assert "CPO光模块" in graph.themes
    assert graph.parent_of("CPO光模块") == "AI硬件"


def test_reason_matching_preserves_multiword_aliases(tmp_path) -> None:
    catalog = tmp_path / "concept_whitelist.yaml"
    catalog.write_text(
        """
AI硬件:
  aliases: [AI]
  members: []
AIPC:
  parent: AI硬件
  aliases: [AI PC]
  members: []
""".lstrip(),
        encoding="utf-8",
    )
    db = tmp_path / "daily.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE ths_hot_reason (date TEXT, code TEXT, reason TEXT)")
        conn.execute(
            "INSERT INTO ths_hot_reason(date, code, reason) VALUES ('2026-06-02', '600000', 'AI PC')"
        )
    graph = ThemeGraph(catalog, db_path=db)

    matches = graph.resolve("600000", "测试股", "", "", datetime(2026, 6, 3, 10, 0))

    assert "AIPC" in {match.theme for match in matches}


def test_old_reason_is_not_promoted_when_recent_scored_dates_have_no_code_row(tmp_path) -> None:
    graph = make_graph(tmp_path, with_db=True)
    with sqlite3.connect(graph.db_path) as conn:
        conn.executemany(
            "INSERT INTO ths_hot_reason(date, code, reason) VALUES (?, ?, ?)",
            [
                ("2026-06-02", "000001", "光纤"),
                ("2026-06-01", "000002", "光纤"),
                ("2026-05-30", "000003", "光纤"),
                ("2026-05-29", "600000", "CPO"),
            ],
        )

    matches = graph.resolve("600000", "测试股", "", "", datetime(2026, 6, 3, 10, 0))

    assert "CPO光模块" not in {match.theme for match in matches}

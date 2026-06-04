from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from stock_codex.market.theme_candidates import ThemeCandidateEngine
from stock_codex.market.theme_graph import ThemeGraph


ROOT = Path(__file__).resolve().parents[1]


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "daily.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    return db


def make_graph(tmp_path: Path, members: str) -> ThemeGraph:
    catalog = tmp_path / "concept_whitelist.yaml"
    catalog.write_text(
        f"""
AI硬件:
  aliases: [AI]
  members: []
CPO光模块:
  parent: AI硬件
  aliases: [CPO, 光模块]
  members:
{members}
""".lstrip(),
        encoding="utf-8",
    )
    return ThemeGraph(catalog, db_path=tmp_path / "daily.db")


def add_stock(
    db: Path,
    code: str,
    *,
    name: str = "测试股",
    board: str = "main",
    is_st: int = 0,
    rows: int = 20,
    amount: float = 300_000_000,
    wide_risk: bool = False,
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO stock_basic(code, name, board, is_st)
               VALUES (?, ?, ?, ?)""",
            (code, name, board, is_st),
        )
        start = date(2026, 5, 1)
        for i in range(rows):
            close = 7.0 if wide_risk and i < rows - 5 else 10.0
            low = 9.5 if wide_risk and i >= rows - 5 else close - 0.1
            conn.execute(
                """INSERT INTO daily_kline(code, date, open, high, low, close, vol, amount, pct_chg)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    (start + timedelta(days=i)).isoformat(),
                    close,
                    close + 0.1,
                    low,
                    close,
                    1_000_000,
                    amount,
                    0.0,
                ),
            )


def snapshot_for(stocks: dict[str, dict]) -> dict:
    return {
        "snapshot_ts": "2026-06-03T10:00:00",
        "is_stale": False,
        "stocks": stocks,
        "theme_strength": {"CPO光模块": {"candidate_allowed": True}},
    }


def test_build_returns_one_anchor_and_one_low_level_follower_with_complete_fields(tmp_path) -> None:
    db = make_db(tmp_path)
    graph = make_graph(tmp_path, "    anchors: [600001]\n    followers: [000001]")
    add_stock(db, "600001", name="锚点")
    add_stock(db, "000001", name="补涨")
    engine = ThemeCandidateEngine(db, graph)

    tickets = engine.build(
        "CPO光模块",
        "T1",
        snapshot_for({
            "600001": {"name": "锚点", "price": 10.0, "pct": 2.0, "amount": 500_000_000},
            "000001": {"name": "补涨", "price": 10.0, "pct": 1.0, "amount": 300_000_000},
        }),
        datetime(2026, 6, 3, 10, 0),
        source_ref="market_state_event:1",
    )

    assert [ticket["candidate_type"] for ticket in tickets] == ["anchor_pullback", "low_level_follower"]
    for ticket in tickets:
        assert ticket["lane"] == "trend"
        assert ticket["origin"] == "theme_candidate"
        assert ticket["entry_low"] == 9.95
        assert ticket["entry_high"] == 10.1
        assert ticket["max_chase_price"] == 10.2
        assert ticket["stop_price"] == 9.9
        assert ticket["size_pct"] == 10
        assert ticket["target_pct"] == 3.0


def test_t0_and_t2_do_not_generate_new_candidates(tmp_path) -> None:
    db = make_db(tmp_path)
    graph = make_graph(tmp_path, "    anchors: [600001]")
    add_stock(db, "600001")
    engine = ThemeCandidateEngine(db, graph)
    snap = snapshot_for({"600001": {"name": "测试股", "price": 10.0, "pct": 1.0, "amount": 300_000_000}})

    assert engine.build("CPO光模块", "T0", snap, datetime(2026, 6, 3, 10, 0), source_ref="x") == []
    assert engine.build("CPO光模块", "T2", snap, datetime(2026, 6, 3, 10, 0), source_ref="x") == []


@pytest.mark.parametrize(
    ("code", "board", "is_st", "pct", "rows", "amount", "wide_risk", "setup"),
    [
        ("000001", "main", 1, 1.0, 20, 300_000_000, False, None),
        ("688001", "star", 0, 1.0, 20, 300_000_000, False, None),
        ("830001", "bse", 0, 1.0, 20, 300_000_000, False, None),
        ("000001", "main", 0, 7.0, 20, 300_000_000, False, None),
        ("000001", "main", 0, 1.0, 19, 300_000_000, False, None),
        ("000001", "main", 0, 1.0, 20, 100_000_000, False, None),
        ("000001", "main", 0, 1.0, 20, 300_000_000, True, None),
        ("000001", "main", 0, 1.0, 20, 300_000_000, False, "limit_up"),
        ("000001", "main", 0, 1.0, 20, 300_000_000, False, "broken"),
    ],
)
def test_disallowed_stocks_never_generate_candidates(
    tmp_path,
    code,
    board,
    is_st,
    pct,
    rows,
    amount,
    wide_risk,
    setup,
) -> None:
    db = make_db(tmp_path)
    graph = make_graph(tmp_path, f"    followers: [{code}]")
    add_stock(db, code, board=board, is_st=is_st, rows=rows, amount=amount, wide_risk=wide_risk)
    with sqlite3.connect(db) as conn:
        if setup == "limit_up":
            conn.execute(
                """INSERT INTO intraday_limit_up_snapshot
                   (snapshot_ts, trade_date, code, name, concept_top1)
                   VALUES ('2026-06-03T10:00:00', '2026-06-03', ?, '测试股', 'CPO光模块')""",
                (code,),
            )
        if setup == "broken":
            conn.execute(
                """INSERT INTO anomaly_event
                   (trade_date, event_key, observed_at, event_time, symbol, code, name, info)
                   VALUES ('2026-06-03', 'broken', '2026-06-03T09:59:00', '09:59:00',
                           '打开涨停板', ?, '测试股', '')""",
                (code,),
            )
    engine = ThemeCandidateEngine(db, graph)

    tickets = engine.build(
        "CPO光模块",
        "T1",
        snapshot_for({code: {"name": "测试股", "price": 10.0, "pct": pct, "amount": 300_000_000}}),
        datetime(2026, 6, 3, 10, 0),
        source_ref="market_state_event:1",
    )

    assert tickets == []


def test_recent_ths_and_same_day_mapped_anomaly_expand_candidate_pool(tmp_path) -> None:
    db = make_db(tmp_path)
    graph = make_graph(tmp_path, "    anchors: []")
    add_stock(db, "000001", name="强势股")
    add_stock(db, "000002", name="异动股")
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO ths_hot_reason(date, code, name, reason)
               VALUES ('2026-06-02', '000001', '强势股', 'CPO')"""
        )
        conn.execute(
            """INSERT INTO anomaly_event
               (trade_date, event_key, observed_at, event_time, symbol, code, name, info)
               VALUES ('2026-06-03', 'event', '2026-06-03T09:59:00', '09:59:00',
                       '火箭发射', '000002', '异动股', '光模块')"""
        )
    engine = ThemeCandidateEngine(db, graph)

    tickets = engine.build(
        "CPO光模块",
        "T1",
        snapshot_for({
            "000001": {"name": "强势股", "price": 10.0, "pct": 1.0, "amount": 300_000_000},
            "000002": {"name": "异动股", "price": 10.0, "pct": 2.0, "amount": 300_000_000},
        }),
        datetime(2026, 6, 3, 10, 0),
        source_ref="market_state_event:1",
    )

    assert len(tickets) == 1
    assert tickets[0]["code"] in {"000001", "000002"}
    assert tickets[0]["candidate_type"] == "low_level_follower"


def test_written_candidate_is_invalidated_after_exceeding_max_chase(tmp_path) -> None:
    db = make_db(tmp_path)
    graph = make_graph(tmp_path, "    anchors: [600001]")
    add_stock(db, "600001", name="锚点")
    engine = ThemeCandidateEngine(db, graph)
    initial = snapshot_for({
        "600001": {"name": "锚点", "price": 10.0, "pct": 2.0, "amount": 300_000_000},
    })
    tickets = engine.build(
        "CPO光模块",
        "T1",
        initial,
        datetime(2026, 6, 3, 10, 0),
        source_ref="market_state_event:1",
    )
    engine.write(tickets, datetime(2026, 6, 3, 10, 0))
    chased = snapshot_for({
        "600001": {"name": "锚点", "price": 10.3, "pct": 4.0, "amount": 300_000_000},
    })

    invalidated = engine.invalidate("CPO光模块", chased, datetime(2026, 6, 3, 10, 5))

    assert invalidated[0]["reason"] == "超过追价上限"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT status FROM decision_tickets WHERE code='600001'"
        ).fetchone()[0] == "invalid"
        assert conn.execute(
            "SELECT COUNT(*) FROM market_state_event WHERE event_type='candidate_invalid'"
        ).fetchone()[0] == 1


def test_theme_cannot_add_second_candidate_of_same_type_later_in_day(tmp_path) -> None:
    db = make_db(tmp_path)
    graph = make_graph(tmp_path, "    followers: [000001, 000002]")
    add_stock(db, "000001", name="补涨一")
    add_stock(db, "000002", name="补涨二")
    engine = ThemeCandidateEngine(db, graph)
    now = datetime(2026, 6, 3, 10, 0)

    first = engine.build(
        "CPO光模块",
        "T1",
        snapshot_for({
            "000001": {"name": "补涨一", "price": 10.0, "pct": 1.0, "amount": 300_000_000},
        }),
        now,
        source_ref="market_state_event:1",
    )
    engine.write(first, now)

    second = engine.build(
        "CPO光模块",
        "T1",
        snapshot_for({
            "000002": {"name": "补涨二", "price": 10.0, "pct": 1.0, "amount": 300_000_000},
        }),
        now + timedelta(minutes=30),
        source_ref="market_state_event:2",
    )

    assert len(first) == 1
    assert second == []

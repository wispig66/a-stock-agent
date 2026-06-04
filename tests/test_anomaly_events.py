from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from stock_codex.market import anomaly_events


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "daily.db"
    sqlite3.connect(db).close()
    anomaly_events.ensure_schema(db)
    return db


def _event(*, code: str = "600000", event_time: str = "10:00:00", info: str = "3分钟涨幅,10.00,3分钟涨幅") -> dict:
    return {
        "symbol": "火箭发射",
        "code": code,
        "name": "浦发银行",
        "event_time": event_time,
        "info": info,
        "sector_hint": "银行",
    }


def test_insert_events_deduplicates_full_snapshot_and_raw_file(tmp_path) -> None:
    db = make_db(tmp_path)
    raw_dir = tmp_path / "anomaly_raw"
    now = datetime(2026, 6, 3, 10, 1, 0)

    first = anomaly_events.insert_events(db, "2026-06-03", now, [_event()], raw_dir=raw_dir)
    second = anomaly_events.insert_events(db, "2026-06-03", now, [_event()], raw_dir=raw_dir)

    assert len(first) == 1
    assert second == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM anomaly_event").fetchone()[0] == 1
    lines = (raw_dir / "20260603.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_key"] == first[0]["event_key"]


def test_consumers_have_independent_persistent_cursors(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 1, 0)
    anomaly_events.insert_events(
        db,
        "2026-06-03",
        now,
        [_event(code="600000"), _event(code="000001")],
    )

    theme_rows = anomaly_events.read_new_events(db, "theme-loop", "2026-06-03")
    anomaly_rows = anomaly_events.read_new_events(db, "stock-anomaly", "2026-06-03")
    anomaly_events.advance_cursor(db, "theme-loop", "2026-06-03", theme_rows[-1]["id"])

    assert [row["code"] for row in theme_rows] == ["600000", "000001"]
    assert [row["code"] for row in anomaly_rows] == ["600000", "000001"]
    assert anomaly_events.read_new_events(db, "theme-loop", "2026-06-03") == []
    assert len(anomaly_events.read_new_events(db, "stock-anomaly", "2026-06-03")) == 2


def test_missing_event_time_still_deduplicates_repeated_snapshot(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 1, 0)

    anomaly_events.insert_events(db, "2026-06-03", now, [_event(event_time="")])
    anomaly_events.insert_events(db, "2026-06-03", now, [_event(event_time="")])

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM anomaly_event").fetchone()[0] == 1


def test_event_time_formats_from_two_daemons_deduplicate(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 1, 0)
    from_time_object = _event()
    from_time_object["event_time"] = datetime(2026, 6, 3, 10, 0, 0)
    from_string = _event(event_time="10:00:00")

    anomaly_events.insert_events(db, "2026-06-03", now, [from_time_object])
    anomaly_events.insert_events(db, "2026-06-03", now, [from_string])

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM anomaly_event").fetchone()[0] == 1


def test_same_event_on_next_trade_date_is_not_deduplicated(tmp_path) -> None:
    db = make_db(tmp_path)
    event = _event()

    anomaly_events.insert_events(db, "2026-06-03", datetime(2026, 6, 3, 10, 1), [event])
    anomaly_events.insert_events(db, "2026-06-04", datetime(2026, 6, 4, 10, 1), [event])

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM anomaly_event").fetchone()[0] == 2

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from stock_codex.tools import analyze_theme_loop


ROOT = Path(__file__).resolve().parents[1]


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "daily.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    return db


def make_catalog(tmp_path: Path) -> Path:
    path = tmp_path / "concepts.yaml"
    path.write_text(
        """
AI硬件:
  aliases: [AI硬件]
CPO光模块:
  parent: AI硬件
  aliases: [CPO, 光模块]
""".strip(),
        encoding="utf-8",
    )
    return path


def insert_snapshot(db: Path, ts: datetime) -> None:
    payload = {
        "snapshot_ts": ts.isoformat(timespec="seconds"),
        "trade_date": ts.strftime("%Y-%m-%d"),
        "is_stale": False,
        "breadth": {"up": 2500, "down": 2200},
        "theme_strength": {
            "CPO光模块": {
                "pct": 5,
                "net_flow": 1,
                "company_count": 3,
                "leader": "样本股",
                "leader_pct": 5,
                "member_count": 3,
                "up_count": 3,
                "strong_count": 4,
            }
        },
        "news": [{"title": "CPO 消息", "themes": ["CPO光模块"]}],
        "stocks": {},
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO market_snapshot(snapshot_ts, trade_date, is_stale, payload_json)
               VALUES (?, ?, 0, ?)""",
            (payload["snapshot_ts"], payload["trade_date"], json.dumps(payload, ensure_ascii=False)),
        )


def insert_event(
    db: Path,
    event_id: int,
    ts: datetime,
    code: str,
    *,
    info: str = "快速拉升",
    event_time: str = "",
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO anomaly_event
               (id, trade_date, event_key, observed_at, event_time, symbol, code, name, sector_hint, info)
               VALUES (?, ?, ?, ?, ?, '火箭发射', ?, ?, '', ?)""",
            (
                event_id,
                ts.strftime("%Y-%m-%d"),
                f"event-{event_id}",
                ts.isoformat(timespec="seconds"),
                event_time,
                code,
                f"样本{event_id}",
                info,
            ),
        )


def test_replay_does_not_use_same_day_postmarket_reason_for_intraday_mapping(tmp_path) -> None:
    db = make_db(tmp_path)
    catalog = make_catalog(tmp_path)
    event_ts = datetime(2026, 6, 3, 10, 0)
    insert_event(db, 1, event_ts, "000001")
    insert_event(db, 2, event_ts, "000002")
    insert_snapshot(db, datetime(2026, 6, 3, 10, 5))
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """INSERT INTO ths_hot_reason(date, code, name, reason)
               VALUES ('2026-06-03', ?, ?, 'CPO')""",
            [("000001", "样本1"), ("000002", "样本2")],
        )

    report = analyze_theme_loop.build_report(db, catalog_path=catalog, single_date="2026-06-03")

    assert report["mapping"]["mapped_events"] == 0
    assert ("2026-06-03", "CPO光模块") not in report["detections"]["T1"]


def test_replay_maps_intraday_event_keywords_and_detects_t1(tmp_path) -> None:
    db = make_db(tmp_path)
    catalog = make_catalog(tmp_path)
    event_ts = datetime(2026, 6, 3, 10, 0)
    insert_event(db, 1, event_ts, "000001", info="CPO 光模块快速拉升")
    insert_event(db, 2, event_ts, "000002", info="CPO 光模块快速拉升")
    insert_snapshot(db, datetime(2026, 6, 3, 10, 5))

    report = analyze_theme_loop.build_report(db, catalog_path=catalog, single_date="2026-06-03")

    assert report["mapping"]["mapping_coverage_pct"] == 100.0
    assert ("2026-06-03", "CPO光模块") in report["detections"]["T1"]
    assert report["discovery_delay"]["t1_median_minutes"] == 5.0


def test_replay_evaluates_new_events_between_market_snapshots(tmp_path) -> None:
    db = make_db(tmp_path)
    catalog = make_catalog(tmp_path)
    insert_snapshot(db, datetime(2026, 6, 3, 9, 35))
    event_ts = datetime(2026, 6, 3, 9, 37)
    insert_event(db, 1, event_ts, "000001", info="CPO 光模块快速拉升")
    insert_event(db, 2, event_ts, "000002", info="CPO 光模块快速拉升")
    insert_snapshot(db, datetime(2026, 6, 3, 9, 40))

    report = analyze_theme_loop.build_report(db, catalog_path=catalog, single_date="2026-06-03")

    detected_at = report["detections"]["T1"][("2026-06-03", "CPO光模块")]
    assert detected_at == event_ts
    assert report["discovery_delay"]["t1_median_minutes"] == 0.0


def test_classification_metrics_exclude_unscored_dates() -> None:
    detections = {
        ("2026-06-03", "CPO光模块"): datetime(2026, 6, 3, 10, 0),
        ("2026-06-04", "电力"): datetime(2026, 6, 4, 10, 0),
    }
    truth = {"2026-06-03": {"CPO光模块", "AI硬件"}}

    metric = analyze_theme_loop.classification_metrics(detections, truth)

    assert metric["predicted"] == 1
    assert metric["actual"] == 2
    assert metric["precision_pct"] == 100.0
    assert metric["recall_pct"] == 50.0
    assert metric["f1_pct"] == 66.67


def test_candidate_metrics_count_near_board_and_chase_violations(tmp_path) -> None:
    db = make_db(tmp_path)
    ts = datetime(2026, 6, 3, 10, 0)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO market_state_event
               (id, event_ts, trade_date, event_type, concept_tag, from_state, to_state, score, payload_json)
               VALUES (1, ?, '2026-06-03', 'T1', 'CPO光模块', 'T0', 'T1', 60, '{}')""",
            (ts.isoformat(timespec="seconds"),),
        )
        conn.execute(
            """INSERT INTO decision_tickets
               (trade_date, code, name, concept, lane, action, entry_low, entry_high,
                max_chase_price, stop_price, target_pct, deadline_time, size_pct,
                status, origin, source_ref)
               VALUES ('2026-06-03', '000001', '样本股', 'CPO光模块', 'trend', 'buy_if',
                       9.90, 10.10, 10.20, 9.70, 3, '14:00', 10,
                       'pending', 'theme_candidate', 'market_state_event:1')"""
        )
        payload = {
            "snapshot_ts": ts.isoformat(timespec="seconds"),
            "trade_date": "2026-06-03",
            "stocks": {"000001": {"name": "样本股", "price": 10.30, "pct": 7.0}},
        }
        conn.execute(
            """INSERT INTO market_snapshot(snapshot_ts, trade_date, is_stale, payload_json)
               VALUES (?, '2026-06-03', 0, ?)""",
            (ts.isoformat(timespec="seconds"), json.dumps(payload, ensure_ascii=False)),
        )

    metrics = analyze_theme_loop.candidate_metrics(db, {"2026-06-03"})

    assert metrics["total"] == 1
    assert metrics["complete_field_rate_pct"] == 100.0
    assert metrics["near_board_count"] == 1
    assert metrics["chase_violation_count"] == 1
    assert metrics["executable_rate_pct"] == 0.0


def test_ticket_time_reads_theme_state_snapshot_source_ref(tmp_path) -> None:
    db = make_db(tmp_path)
    ticket = {
        "source_ref": "theme_state_snapshot:2026-06-03T10:05:00",
        "created_at": "2026-06-03T10:30:00",
    }
    with sqlite3.connect(db) as conn:
        at = analyze_theme_loop._ticket_time(conn, ticket)

    assert at == datetime(2026, 6, 3, 10, 5)


def test_missing_event_time_falls_back_to_observed_at() -> None:
    event = {
        "trade_date": "2026-06-03",
        "observed_at": "2026-06-03T10:05:00",
        "event_time": "",
    }

    assert analyze_theme_loop.event_effective_time(event) == datetime(2026, 6, 3, 10, 5)

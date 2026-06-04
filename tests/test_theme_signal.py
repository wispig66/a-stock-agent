from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from stock_codex.market.theme_signal import ThemeSignal


def snapshot(
    ts: datetime,
    *,
    stale: bool = False,
    strengths: dict | None = None,
    anchors: dict | None = None,
    news: list | None = None,
    overseas: dict | None = None,
) -> dict:
    return {
        "snapshot_ts": ts.isoformat(timespec="seconds"),
        "is_stale": stale,
        "theme_strength": strengths or {},
        "anchors": anchors or {},
        "news": news or [],
        "overseas": overseas or {},
    }


def events(n: int) -> list[dict]:
    return [{"event_key": f"e{i}"} for i in range(n)]


def strong_stats(*, pct: float = 4.0, leader_pct: float = 6.0) -> dict:
    return {
        "pct": pct,
        "net_flow": 100.0,
        "company_count": 20,
        "leader_pct": leader_pct,
        "member_count": 4,
        "up_count": 4,
        "strong_count": 2,
        "avg_pct": 3.0,
    }


def test_t1_cannot_trigger_before_0935(tmp_path: Path) -> None:
    db = tmp_path / "daily.db"
    signal = ThemeSignal(db)
    now = datetime(2026, 6, 3, 9, 34)
    snap = snapshot(now, strengths={"CPO光模块": strong_stats()})

    evaluations, transitions = signal.evaluate(
        now,
        snap,
        {"CPO光模块": events(4)},
        limit_up_counts={"CPO光模块": 0},
    )

    assert evaluations[0].score >= 55
    assert evaluations[0].state == "T0"
    assert transitions[0]["to_state"] == "T0"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT queue_status FROM market_state_event WHERE id=?",
            (transitions[0]["id"],),
        ).fetchone()[0] == "short_only"


def test_t2_cannot_jump_past_opening_gate_before_0935(tmp_path: Path) -> None:
    signal = ThemeSignal(tmp_path / "daily.db")
    now = datetime(2026, 6, 3, 9, 34)
    snap = snapshot(
        now,
        strengths={"CPO光模块": strong_stats(pct=8, leader_pct=12)},
        news=[{"themes": ["CPO光模块"]}],
        overseas={"MRVL": {"pct": 3.0, "themes": ["CPO光模块"]}},
    )

    evaluations, transitions = signal.evaluate(
        now,
        snap,
        {"CPO光模块": events(8)},
        limit_up_counts={"CPO光模块": 3},
    )

    assert evaluations[0].score >= 80
    assert evaluations[0].state == "T0"
    assert transitions[0]["event_type"] == "T0"


def test_t1_requires_flow_and_breadth_or_anchor(tmp_path: Path) -> None:
    signal = ThemeSignal(tmp_path / "daily.db")
    now = datetime(2026, 6, 3, 9, 35)
    snap = snapshot(now, strengths={"CPO光模块": strong_stats()})

    evaluations, transitions = signal.evaluate(
        now,
        snap,
        {"CPO光模块": events(3)},
        limit_up_counts={"CPO光模块": 0},
    )

    result = evaluations[0]
    assert result.state == "T1"
    assert result.components["anomaly_flow"] >= 10
    assert result.components["breadth"] >= 8
    assert transitions[0]["event_type"] == "T1"


def test_stale_snapshot_cannot_promote_signal(tmp_path: Path) -> None:
    signal = ThemeSignal(tmp_path / "daily.db")
    now = datetime(2026, 6, 3, 9, 40)
    snap = snapshot(now, stale=True, strengths={"CPO光模块": strong_stats(pct=8, leader_pct=12)})

    evaluations, transitions = signal.evaluate(
        now,
        snap,
        {"CPO光模块": events(8)},
        limit_up_counts={"CPO光模块": 3},
    )

    assert evaluations[0].score >= 80
    assert evaluations[0].state == "NONE"
    assert transitions == []


def test_t2_triggers_on_single_score_at_least_80(tmp_path: Path) -> None:
    signal = ThemeSignal(tmp_path / "daily.db")
    now = datetime(2026, 6, 3, 9, 40)
    snap = snapshot(
        now,
        strengths={"CPO光模块": strong_stats(pct=8, leader_pct=12)},
        news=[{"themes": ["CPO光模块"]}],
        overseas={"MRVL": {"pct": 3.0, "themes": ["CPO光模块"]}},
    )

    evaluations, transitions = signal.evaluate(
        now,
        snap,
        {"CPO光模块": events(8)},
        limit_up_counts={"CPO光模块": 3},
    )

    assert evaluations[0].state == "T2"
    assert transitions[0]["event_type"] == "T2"


def test_t2_triggers_after_two_distinct_snapshots_at_least_70(tmp_path: Path) -> None:
    signal = ThemeSignal(tmp_path / "daily.db")
    first = datetime(2026, 6, 3, 9, 40)
    second = first + timedelta(minutes=5)
    stats = {"CPO光模块": strong_stats(pct=5, leader_pct=8)}

    one, _ = signal.evaluate(first, snapshot(first, strengths=stats), {"CPO光模块": events(5)}, limit_up_counts={})
    two, transitions = signal.evaluate(second, snapshot(second, strengths=stats), {"CPO光模块": events(5)}, limit_up_counts={})

    assert 70 <= one[0].score < 80
    assert one[0].state == "T1"
    assert two[0].state == "T2"
    assert transitions[0]["event_type"] == "T2"


def test_theme_cools_after_two_low_score_snapshots(tmp_path: Path) -> None:
    signal = ThemeSignal(tmp_path / "daily.db")
    start = datetime(2026, 6, 3, 9, 40)
    signal.evaluate(
        start,
        snapshot(start, strengths={"CPO光模块": strong_stats()}),
        {"CPO光模块": events(3)},
        limit_up_counts={},
    )

    low1, _ = signal.evaluate(start + timedelta(minutes=5), snapshot(start + timedelta(minutes=5)), {}, limit_up_counts={})
    low2, transitions = signal.evaluate(start + timedelta(minutes=10), snapshot(start + timedelta(minutes=10)), {}, limit_up_counts={})

    assert low1[0].state == "T1"
    assert low2[0].state == "COOLING"
    assert transitions[0]["event_type"] == "cooling"


def test_theme_rotates_out_when_another_theme_leads_by_15(tmp_path: Path) -> None:
    signal = ThemeSignal(tmp_path / "daily.db")
    start = datetime(2026, 6, 3, 9, 40)
    signal.evaluate(
        start,
        snapshot(start, strengths={"电力": strong_stats()}),
        {"电力": events(3)},
        limit_up_counts={},
    )
    later = start + timedelta(minutes=5)
    snap = snapshot(later, strengths={"AI硬件": strong_stats(pct=8, leader_pct=12)})

    evaluations, transitions = signal.evaluate(
        later,
        snap,
        {"AI硬件": events(6), "电力": events(2)},
        limit_up_counts={},
    )

    by_theme = {result.theme: result for result in evaluations}
    assert by_theme["电力"].score < 55
    assert by_theme["AI硬件"].score - by_theme["电力"].score >= 15
    assert by_theme["电力"].state == "ROTATED"
    assert any(event["event_type"] == "rotation" for event in transitions)


def test_short_alert_cooldown_and_daily_limit(tmp_path: Path) -> None:
    signal = ThemeSignal(tmp_path / "daily.db")
    now = datetime(2026, 6, 3, 9, 40)
    event_ids = []
    for i in range(9):
        theme = f"题材{i}"
        _, transitions = signal.evaluate(
            now + timedelta(seconds=i),
            snapshot(now + timedelta(seconds=i), strengths={theme: strong_stats()}),
            {theme: events(1)},
            limit_up_counts={},
        )
        event_ids.append(transitions[0]["id"])

    for event_id in event_ids[:8]:
        assert signal.can_push_short(event_id, now) is True
        signal.mark_short_pushed(event_id, now)

    assert signal.can_push_short(event_ids[8], now) is False
    assert signal.can_push_short(event_ids[0], now + timedelta(minutes=5)) is False

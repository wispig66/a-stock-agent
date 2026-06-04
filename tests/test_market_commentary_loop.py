from __future__ import annotations

import json
import importlib.util
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import stock_codex.apps.market_commentary_loop as market_commentary_loop
from stock_codex.apps.market_commentary_loop import (
    MarketCommentaryLoop,
    acquire_process_lock,
    in_worker_window,
)


ROOT = Path(__file__).resolve().parents[1]
FACT_PACK_PATH = (
    ROOT / ".agents" / "skills" / "stock-market-dynamic" / "scripts" / "build_fact_pack.py"
)


def load_fact_pack_module():
    spec = importlib.util.spec_from_file_location("stock_market_dynamic_fact_pack", FACT_PACK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "daily.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    return db


def insert_event(db: Path, ts: datetime, *, theme: str = "CPO光模块", event_type: str = "T1") -> int:
    payload = {"theme": theme, "event_type": event_type}
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            """INSERT INTO market_state_event
               (event_ts, trade_date, event_type, concept_tag, from_state, to_state, score, payload_json)
               VALUES (?, ?, ?, ?, 'NONE', 'T1', 60, ?)""",
            (
                ts.isoformat(timespec="seconds"),
                ts.strftime("%Y-%m-%d"),
                event_type,
                theme,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def insert_push(db: Path, ts: datetime) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO push_log(timestamp, source, text, success)
               VALUES (?, 'stock-market-dynamic', 'card', 1)""",
            (ts.isoformat(timespec="seconds"),),
        )


def test_worker_coalesces_three_minutes_and_marks_batch_done(tmp_path) -> None:
    db = make_db(tmp_path)
    first = datetime(2026, 6, 3, 10, 0)
    first_id = insert_event(db, first)
    second_id = insert_event(db, first + timedelta(minutes=2), theme="电力")
    calls = []

    def invoke(event_ids, timeout):
        calls.append((event_ids, timeout))
        insert_push(db, first + timedelta(minutes=3))
        return 0

    worker = MarketCommentaryLoop(db, tmp_path, invoke_codex=invoke)

    assert worker.process_once(first + timedelta(minutes=2, seconds=59)) == "coalescing"
    assert worker.process_once(first + timedelta(minutes=3)) == "done"
    assert calls[0][0] == [first_id, second_id]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM market_state_event WHERE queue_status='done'"
        ).fetchone()[0] == 2


def test_worker_respects_full_card_cooldown_and_daily_limit(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 30)
    insert_push(db, now - timedelta(minutes=10))
    event_id = insert_event(db, now - timedelta(minutes=5))
    worker = MarketCommentaryLoop(db, tmp_path, invoke_codex=lambda ids, timeout: 0)

    assert worker.process_once(now) == "cooldown"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT queue_status FROM market_state_event WHERE id=?",
            (event_id,),
        ).fetchone()[0] == "pending"

    for minutes in (40, 60, 80):
        insert_push(db, now - timedelta(minutes=minutes))
    assert worker.process_once(now + timedelta(minutes=11)) == "daily_limit"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT queue_status FROM market_state_event WHERE id=?",
            (event_id,),
        ).fetchone()[0] == "suppressed"


def test_worker_retries_once_after_ten_minutes_then_succeeds(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 10)
    event_id = insert_event(db, now - timedelta(minutes=5))
    calls = 0

    def invoke(event_ids, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 1
        insert_push(db, now + timedelta(minutes=10))
        return 0

    worker = MarketCommentaryLoop(db, tmp_path, invoke_codex=invoke)

    assert worker.process_once(now) == "retry"
    assert worker.process_once(now + timedelta(minutes=9)) == "no_events"
    assert worker.process_once(now + timedelta(minutes=10)) == "done"
    with sqlite3.connect(db) as conn:
        status, retry_count = conn.execute(
            "SELECT queue_status, retry_count FROM market_state_event WHERE id=?",
            (event_id,),
        ).fetchone()
    assert status == "done"
    assert retry_count == 1


def test_worker_treats_missing_validated_push_as_failure(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 10)
    event_id = insert_event(db, now - timedelta(minutes=5))
    worker = MarketCommentaryLoop(db, tmp_path, invoke_codex=lambda ids, timeout: 0)

    assert worker.process_once(now) == "retry"
    with sqlite3.connect(db) as conn:
        status, error = conn.execute(
            "SELECT queue_status, error FROM market_state_event WHERE id=?",
            (event_id,),
        ).fetchone()
    assert status == "retry"
    assert "validated push" in error


def test_worker_recovers_stale_processing_event(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 30)
    event_id = insert_event(db, now - timedelta(minutes=20))
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE market_state_event
               SET queue_status='processing', processing_started_at=?
               WHERE id=?""",
            ((now - timedelta(minutes=11)).isoformat(timespec="seconds"), event_id),
        )

    def invoke(event_ids, timeout):
        insert_push(db, now)
        return 0

    worker = MarketCommentaryLoop(db, tmp_path, invoke_codex=invoke)

    assert worker.process_once(now) == "done"
    with sqlite3.connect(db) as conn:
        status, retry_count = conn.execute(
            "SELECT queue_status, retry_count FROM market_state_event WHERE id=?",
            (event_id,),
        ).fetchone()
    assert status == "done"
    assert retry_count == 1


def test_worker_ignores_t0_short_only_events_even_if_pending(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 10)
    insert_event(db, now - timedelta(minutes=5), event_type="T0")
    worker = MarketCommentaryLoop(db, tmp_path, invoke_codex=lambda ids, timeout: 0)

    assert worker.process_once(now) == "no_events"


def test_process_lock_is_non_blocking(tmp_path) -> None:
    lock_path = tmp_path / "market_commentary.lock"

    first = acquire_process_lock(lock_path)
    second = acquire_process_lock(lock_path)

    assert first is not None
    assert second is None
    first.close()


def test_worker_window_drains_late_session_events() -> None:
    assert in_worker_window(datetime(2026, 6, 3, 11, 32)) is True
    assert in_worker_window(datetime(2026, 6, 3, 15, 2)) is True
    assert in_worker_window(datetime(2026, 6, 3, 11, 46)) is False
    assert in_worker_window(datetime(2026, 6, 3, 15, 16)) is False


def test_worker_suppresses_open_events_when_day_drain_ends(tmp_path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 15, 16)
    pending_id = insert_event(db, now - timedelta(minutes=5))
    retry_id = insert_event(db, now - timedelta(minutes=4), theme="电力")
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE market_state_event
               SET queue_status='retry', retry_count=1, next_retry_at=?
               WHERE id=?""",
            ((now + timedelta(minutes=5)).isoformat(timespec="seconds"), retry_id),
        )

    worker = MarketCommentaryLoop(db, tmp_path, invoke_codex=lambda ids, timeout: 0)
    worker.suppress_open_events(now, "session drain window ended")

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            """SELECT queue_status, error FROM market_state_event
               WHERE id IN (?, ?) ORDER BY id""",
            (pending_id, retry_id),
        ).fetchall()
    assert rows == [
        ("suppressed", "session drain window ended"),
        ("suppressed", "session drain window ended"),
    ]


def test_worker_codex_command_is_read_only_and_ephemeral(tmp_path) -> None:
    worker = MarketCommentaryLoop(make_db(tmp_path), tmp_path)

    cmd = worker._codex_command(tmp_path / "last_message.txt")

    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--ask-for-approval") + 1] == "never"
    assert cmd.index("--ask-for-approval") < cmd.index("exec")
    assert "--ephemeral" in cmd
    assert "--ignore-user-config" in cmd
    assert "--disable" in cmd
    for feature in (
        "shell_tool",
        "unified_exec",
        "browser_use",
        "computer_use",
        "apps",
        "plugins",
        "hooks",
        "skill_mcp_dependency_install",
        "request_permissions_tool",
        "tool_call_mcp_elicitation",
    ):
        assert feature in cmd


def test_default_worker_pushes_only_after_read_only_codex_output(tmp_path, monkeypatch) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 6, 3, 10, 10)
    event_id = insert_event(db, now - timedelta(minutes=5))
    allowed = {
        "schema_version": "2",
        "skill": "stock-market-dynamic",
        "summary": {"date": "2026-06-03"},
        "codes": {},
        "pct": {},
    }
    card = "\n".join([
        "市场主线",
        "弱势与轮动",
        "锚点",
        "持仓与票池",
        "可执行候选",
    ])
    calls = []
    pushed = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "build_fact_pack.py" in " ".join(cmd):
            stdout = f"=== ALLOWED ===\n{json.dumps(allowed)}\n=== /ALLOWED ===\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text(card, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_push(text, *, source):
        pushed.append((text, source))
        insert_push(db, now)
        return {"result": {"message_id": 1}}

    monkeypatch.setattr(market_commentary_loop, "run_subprocess", fake_run)
    monkeypatch.setattr(market_commentary_loop, "push_one", fake_push)
    worker = MarketCommentaryLoop(db, tmp_path)

    assert worker.process_once(now) == "done"
    assert pushed == [(card, "stock-market-dynamic")]
    codex_cmd = calls[1]
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex_cmd
    assert codex_cmd[codex_cmd.index("--sandbox") + 1] == "read-only"
    assert event_id > 0


def test_card_shape_rejects_non_actionable_code_in_candidate_section() -> None:
    card = """
市场主线
弱势与轮动
锚点
- 300308 中际旭创
持仓与票池
- 无
可执行候选
- 300308 中际旭创 · 观察
""".strip()
    allowed = {
        "summary": {"snapshot_stale": False},
        "codes": {"300308": "中际旭创"},
        "actionable_candidates": [],
        "concentration_inference_allowed": False,
    }

    with pytest.raises(ValueError, match="候选栏包含非可执行代码"):
        MarketCommentaryLoop._validate_card_shape(card, allowed)


def test_card_shape_requires_stale_snapshot_warning() -> None:
    card = "\n".join([
        "市场主线",
        "弱势与轮动",
        "锚点",
        "持仓与票池",
        "可执行候选",
        "无新增可执行候选",
    ])
    allowed = {
        "summary": {"snapshot_stale": True},
        "codes": {},
        "actionable_candidates": [],
        "concentration_inference_allowed": False,
    }

    with pytest.raises(ValueError, match="快照已过期"):
        MarketCommentaryLoop._validate_card_shape(card, allowed)


def test_card_shape_rejects_concentration_synonym_without_evidence() -> None:
    card = "\n".join([
        "市场主线",
        "弱势与轮动",
        "资金虹吸其他板块",
        "锚点",
        "持仓与票池",
        "可执行候选",
        "无新增可执行候选",
    ])
    allowed = {
        "summary": {"snapshot_stale": False},
        "codes": {},
        "actionable_candidates": [],
        "concentration_inference_allowed": False,
    }

    with pytest.raises(ValueError, match="资金集中推断"):
        MarketCommentaryLoop._validate_card_shape(card, allowed)


def test_concentration_inference_requires_all_three_conditions() -> None:
    fact_pack = load_fact_pack_module()
    events = [
        {"event_type": "T1", "concept_tag": "AI硬件"},
        {"event_type": "cooling", "concept_tag": "电力"},
    ]
    states = [{"theme": "AI硬件", "state": "T1", "score": 70}]
    current = {
        "is_stale": False,
        "breadth": {"up": 1800, "down": 3200},
        "theme_strength": {"AI硬件": {"net_flow": 10}},
    }
    previous = {"theme_strength": {"AI硬件": {"net_flow": 5}}}

    allowed, evidence = fact_pack.concentration_inference(events, states, current, previous)
    assert allowed is True
    assert evidence["cooling_themes"] == ["电力"]

    current["breadth"] = {"up": 3200, "down": 1800}
    allowed, _ = fact_pack.concentration_inference(events, states, current, previous)
    assert allowed is False


def test_concentration_inference_requires_previous_theme_flow_evidence() -> None:
    fact_pack = load_fact_pack_module()
    events = [
        {"event_type": "T1", "concept_tag": "AI硬件"},
        {"event_type": "cooling", "concept_tag": "电力"},
    ]
    states = [{"theme": "AI硬件", "state": "T1", "score": 70}]
    current = {
        "is_stale": False,
        "breadth": {"up": 1800, "down": 3200},
        "theme_strength": {"AI硬件": {"net_flow": 10}},
    }

    allowed, evidence = fact_pack.concentration_inference(events, states, current, {})

    assert allowed is False
    assert evidence["flow_strengthened"] is False


def test_fact_pack_excludes_near_limit_or_over_chase_candidates() -> None:
    fact_pack = load_fact_pack_module()
    tickets = [
        {
            "code": "000001",
            "origin": "theme_candidate",
            "lane": "trend",
            "status": "pending",
            "max_chase_price": 10.2,
            "stop_price": 9.5,
            "deadline_time": "14:00",
        },
        {
            "code": "000002",
            "origin": "theme_candidate",
            "lane": "trend",
            "status": "pending",
            "max_chase_price": 10.2,
            "stop_price": 9.5,
            "deadline_time": "14:00",
        },
        {
            "code": "000003",
            "origin": "theme_candidate",
            "lane": "trend",
            "status": "pending",
            "max_chase_price": 10.2,
            "stop_price": 9.5,
            "deadline_time": "14:00",
        },
    ]
    snapshot = {
        "is_stale": False,
        "stocks": {
            "000001": {"price": 10.0, "pct": 2.0},
            "000002": {"price": 10.0, "pct": 7.0},
            "000003": {"price": 10.3, "pct": 2.0},
        }
    }

    assert fact_pack._actionable_candidates(
        tickets,
        snapshot,
        datetime(2026, 6, 3, 13, 59),
    ) == [tickets[0]]


def test_fact_pack_excludes_stale_expired_or_below_stop_candidates() -> None:
    fact_pack = load_fact_pack_module()
    now = datetime(2026, 6, 3, 14, 1)
    ticket = {
        "code": "000001",
        "origin": "theme_candidate",
        "lane": "trend",
        "status": "pending",
        "max_chase_price": 10.2,
        "stop_price": 9.5,
        "deadline_time": "14:00",
    }

    stale_snapshot = {
        "is_stale": True,
        "stocks": {"000001": {"price": 10.0, "pct": 2.0}},
    }
    assert fact_pack._actionable_candidates([ticket], stale_snapshot, now) == []

    fresh_snapshot = {
        "is_stale": False,
        "stocks": {"000001": {"price": 10.0, "pct": 2.0}},
    }
    assert fact_pack._actionable_candidates([ticket], fresh_snapshot, now) == []

    ticket["deadline_time"] = "14:30"
    fresh_snapshot["stocks"]["000001"]["price"] = 9.4
    assert fact_pack._actionable_candidates([ticket], fresh_snapshot, now) == []


def test_fact_pack_marks_old_snapshot_stale_as_of_generation(tmp_path) -> None:
    fact_pack = load_fact_pack_module()
    db = make_db(tmp_path)
    snapshot_at = datetime(2026, 6, 3, 10, 0)
    event_id = insert_event(db, snapshot_at)
    payload = {
        "snapshot_ts": snapshot_at.isoformat(timespec="seconds"),
        "trade_date": "2026-06-03",
        "is_stale": False,
        "stocks": {},
        "theme_strength": {},
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO market_snapshot(snapshot_ts, trade_date, is_stale, payload_json)
               VALUES (?, ?, 0, ?)""",
            (
                snapshot_at.isoformat(timespec="seconds"),
                "2026-06-03",
                json.dumps(payload, ensure_ascii=False),
            ),
        )

    allowed = fact_pack.build_fact_pack(
        db,
        [event_id],
        as_of=snapshot_at + timedelta(minutes=11),
        holdings=[],
    )

    assert allowed["summary"]["snapshot_stale"] is True

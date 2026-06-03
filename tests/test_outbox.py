from __future__ import annotations

import sqlite3
from pathlib import Path

from stock_codex.channels import ChannelGateway, MockAdapter
from stock_codex.channels.outbox import OutboxStore, drain_once

ROOT = Path(__file__).resolve().parents[1]


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.commit()
    conn.close()
    return path


def test_connection_bound_send_enqueues_instead_of_sending(tmp_path):
    db = _db(tmp_path / "t.db")
    adapter = MockAdapter(connection_bound=True)
    gateway = ChannelGateway({"mock": adapter}, default_channel="mock", db_path=db)

    delivery = gateway.send_text("hello", source="unit", format="markdown")

    # queued: no provider id yet, adapter not called, nothing in outbound log
    assert delivery.provider_message_id == ""
    assert delivery.raw["queued"] is True
    assert adapter.sent == []
    with sqlite3.connect(db) as conn:
        outbox = conn.execute(
            "SELECT channel, target, text, format, source, status, attempts FROM channel_outbox"
        ).fetchone()
        outbound = conn.execute("SELECT COUNT(*) FROM channel_outbound_log").fetchone()
    assert outbox == ("mock", "mock-conversation", "hello", "markdown", "unit", "pending", 0)
    assert outbound[0] == 0


def test_stateless_send_still_sends_directly(tmp_path):
    db = _db(tmp_path / "t.db")
    adapter = MockAdapter(connection_bound=False)
    gateway = ChannelGateway({"mock": adapter}, default_channel="mock", db_path=db)

    delivery = gateway.send_text("hi", source="unit")

    assert delivery.provider_message_id == "1"
    assert adapter.sent[0]["text"] == "hi"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM channel_outbound_log").fetchone()[0] == 1


def test_drain_sends_pending_and_logs_outbound(tmp_path):
    db = _db(tmp_path / "t.db")
    gateway = ChannelGateway({}, default_channel="wecom", db_path=db)
    store = OutboxStore(db)
    store.enqueue(channel="wecom", target="u1", text="card", format="markdown", source="stock-premarket")

    captured = []

    def sender(target, text, fmt):
        captured.append((target, text, fmt))
        return "wx-99"

    sent = drain_once(store, {"wecom": sender}, logger=gateway)

    assert sent == 1
    assert captured == [("u1", "card", "markdown")]
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, provider_msg_id, attempts FROM channel_outbox"
        ).fetchone()
        log = conn.execute(
            "SELECT channel, provider_msg_id, source, text, success FROM channel_outbound_log"
        ).fetchone()
    assert row == ("sent", "wx-99", 1)
    assert log == ("wecom", "wx-99", "stock-premarket", "card", 1)


def test_drain_retries_then_fails_after_max_attempts(tmp_path):
    db = _db(tmp_path / "t.db")
    gateway = ChannelGateway({}, default_channel="wecom", db_path=db)
    store = OutboxStore(db)
    store.enqueue(channel="wecom", target="u1", text="x", source="unit")

    def boom(target, text, fmt):
        raise RuntimeError("ws down")

    # 2 attempts cap: first drain -> pending(attempts=1), second -> failed(attempts=2)
    assert drain_once(store, {"wecom": boom}, logger=gateway, max_attempts=2) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status, attempts FROM channel_outbox").fetchone() == ("pending", 1)
        assert conn.execute("SELECT COUNT(*) FROM channel_outbound_log").fetchone()[0] == 0

    assert drain_once(store, {"wecom": boom}, logger=gateway, max_attempts=2) == 0
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status, attempts, last_error FROM channel_outbox").fetchone()
        log = conn.execute("SELECT success, error FROM channel_outbound_log").fetchone()
    assert row[0] == "failed" and row[1] == 2 and "ws down" in row[2]
    assert log == (0, "ws down")

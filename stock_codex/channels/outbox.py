"""Outbox relay for connection-bound channels.

WeCom AI Bot (WS) and WeChat iLink can only send through the persistent
connection held by the listener process. Other processes (cron pushes, skill
runs) therefore enqueue here; the listener's drain thread consumes pending rows
and sends them via a registered sender.

Layers:
- ``OutboxStore`` — SQLite-backed queue (enqueue / fetch_pending / mark_*).
- ``register_outbox_sender`` — platform listeners advertise their live sender.
- ``run_outbox_drain`` — loop the listener runs to flush the queue.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from stock_codex.infra.db import connect_close
from stock_codex.paths import DB_FILE


@dataclass(frozen=True)
class OutboxRow:
    id: int
    channel: str
    account_id: str | None
    target: str
    text: str
    format: str
    source: str | None
    attempts: int


# A sender takes (target, text, format) and returns the provider message id.
Sender = Callable[[str, str, str], str]


class OutboxStore:
    def __init__(self, db_path: Path = DB_FILE) -> None:
        self.db_path = db_path

    def enqueue(
        self,
        *,
        channel: str,
        target: str,
        text: str,
        format: str = "plain",
        account_id: str | None = None,
        source: str | None = None,
    ) -> int:
        with connect_close(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO channel_outbox
                   (created_at, channel, account_id, target, text, format, source, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    channel,
                    account_id,
                    target,
                    text,
                    format,
                    source,
                ),
            )
            return cur.lastrowid

    def fetch_pending(self, channel: str, *, limit: int = 20) -> list[OutboxRow]:
        with connect_close(self.db_path) as conn:
            rows = conn.execute(
                """SELECT id, channel, account_id, target, text, format, source, attempts
                   FROM channel_outbox
                   WHERE channel = ? AND status = 'pending'
                   ORDER BY id ASC LIMIT ?""",
                (channel, limit),
            ).fetchall()
        return [
            OutboxRow(
                id=r[0], channel=r[1], account_id=r[2], target=r[3],
                text=r[4], format=r[5], source=r[6], attempts=r[7],
            )
            for r in rows
        ]

    def mark_sent(self, row_id: int, provider_msg_id: str) -> None:
        with connect_close(self.db_path) as conn:
            conn.execute(
                """UPDATE channel_outbox
                   SET status='sent', provider_msg_id=?, sent_at=?, attempts=attempts+1,
                       last_error=NULL
                   WHERE id=?""",
                (provider_msg_id, datetime.now().isoformat(timespec="seconds"), row_id),
            )

    def mark_failed(self, row_id: int, error: str, *, max_attempts: int) -> str:
        """Bump attempts; flip to 'failed' once the cap is reached. Returns new status."""
        with connect_close(self.db_path) as conn:
            row = conn.execute(
                "SELECT attempts FROM channel_outbox WHERE id=?", (row_id,)
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            status = "failed" if attempts >= max_attempts else "pending"
            conn.execute(
                "UPDATE channel_outbox SET status=?, attempts=?, last_error=? WHERE id=?",
                (status, attempts, error[:500], row_id),
            )
        return status


# Platform listeners register their live sender here on connect; the drain loop
# reads it. Keyed by channel name.
_SENDERS: dict[str, Sender] = {}
_SENDERS_LOCK = threading.Lock()


def register_outbox_sender(channel: str, sender: Sender) -> None:
    with _SENDERS_LOCK:
        _SENDERS[channel] = sender


def unregister_outbox_sender(channel: str) -> None:
    with _SENDERS_LOCK:
        _SENDERS.pop(channel, None)


def current_senders() -> dict[str, Sender]:
    with _SENDERS_LOCK:
        return dict(_SENDERS)


class _OutboundLogger(Protocol):
    def record_outbound(
        self, *, channel: str, account_id: str | None, target: str,
        provider_msg_id: str, source: str | None, text: str, format: str,
        success: bool, error: str | None,
    ) -> None:
        ...


def retry_backoff_seconds(attempts: int, *, base: float = 2.0, cap: float = 300.0) -> float:
    """Exponential backoff for a row that has failed ``attempts`` times (>=1).

    2s, 4s, 8s, … capped at 5min. Keeps a flapping connection from burning
    through ``max_attempts`` in seconds while still giving up on a truly dead row.
    """
    return min(base * (2 ** max(attempts - 1, 0)), cap)


def drain_once(
    store: OutboxStore,
    senders: dict[str, Sender],
    *,
    logger: _OutboundLogger | None = None,
    limit: int = 20,
    max_attempts: int = 8,
    retry_state: dict[int, float] | None = None,
    now: float | None = None,
) -> int:
    """Flush one batch per registered sender. Returns number of rows sent.

    ``retry_state`` (row_id -> earliest next-attempt epoch) holds the in-memory
    backoff schedule for rows that failed transiently. It is intentionally NOT
    persisted: on process restart or reconnect it resets, so every pending row
    is retried immediately ("resend after recovery"). Terminal rows (sent or
    permanently failed) are evicted so the dict stays bounded by pending rows.
    """
    now = time.time() if now is None else now
    sent = 0
    for channel, sender in senders.items():
        for row in store.fetch_pending(channel, limit=limit):
            if retry_state is not None:
                due = retry_state.get(row.id)
                if due is not None and due > now:
                    continue  # still backing off
            try:
                provider_msg_id = sender(row.target, row.text, row.format)
                store.mark_sent(row.id, provider_msg_id or "")
                if retry_state is not None:
                    retry_state.pop(row.id, None)
                sent += 1
                if logger is not None:
                    logger.record_outbound(
                        channel=row.channel, account_id=row.account_id, target=row.target,
                        provider_msg_id=provider_msg_id or "", source=row.source,
                        text=row.text, format=row.format, success=True, error=None,
                    )
            except Exception as e:  # noqa: BLE001 - sender failures must not kill the loop
                status = store.mark_failed(row.id, str(e), max_attempts=max_attempts)
                if status == "failed":
                    if retry_state is not None:
                        retry_state.pop(row.id, None)
                    if logger is not None:
                        logger.record_outbound(
                            channel=row.channel, account_id=row.account_id, target=row.target,
                            provider_msg_id="", source=row.source, text=row.text,
                            format=row.format, success=False, error=str(e),
                        )
                elif retry_state is not None:
                    retry_state[row.id] = now + retry_backoff_seconds(row.attempts + 1)
    return sent


def run_outbox_drain(
    *,
    db_path: Path = DB_FILE,
    logger: _OutboundLogger | None = None,
    poll_interval: float = 1.0,
    max_attempts: int = 8,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Long-running drain loop for the listener process.

    Idle-cheap: when no senders are registered (no connection-bound channel
    enabled) it just sleeps. Senders appear once their listener connects.

    ``retry_state`` lives for the life of the loop so backoff is honored across
    polls; it resets on restart, retrying all pending rows immediately.
    """
    store = OutboxStore(db_path)
    stop = should_stop or (lambda: False)
    retry_state: dict[int, float] = {}
    while not stop():
        try:
            senders = current_senders()
            if senders:
                drain_once(
                    store, senders, logger=logger,
                    max_attempts=max_attempts, retry_state=retry_state,
                )
        except Exception:
            # Never let a transient DB error kill the daemon thread.
            pass
        time.sleep(poll_interval)

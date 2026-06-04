"""共享异动事件事实库。

两个盘中 daemon 都可以拉取全量异动快照，但只有首次出现的事件会写入 SQLite
和 JSONL。下游通过独立 consumer cursor 消费，避免并发、重启和全量快照重复。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from stock_codex.infra.db import connect_close


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS anomaly_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    observed_at TEXT NOT NULL,
    event_time TEXT,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    sector_hint TEXT,
    info TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_anomaly_event_date_id
    ON anomaly_event(trade_date, id);
CREATE INDEX IF NOT EXISTS idx_anomaly_event_date_code
    ON anomaly_event(trade_date, code);

CREATE TABLE IF NOT EXISTS anomaly_consumer_cursor (
    consumer TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (consumer, trade_date)
);
"""
TIME_RE = re.compile(r"(?<!\d)(\d{2}:\d{2}(?::\d{2})?)(?!\d)")


def ensure_schema(db_path: str | Path) -> None:
    """创建共享事件表；可由 daemon 在启动时幂等调用。"""
    with connect_close(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _event_time(value) -> str:
    if value is None or value == "":
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M:%S")
    raw = _text(value)
    if len(raw) == 6 and raw.isdigit():
        raw = f"{raw[:2]}:{raw[2:4]}:{raw[4:6]}"
    else:
        match = TIME_RE.search(raw)
        if match:
            raw = match.group(1)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.strftime("%H:%M:%S")
        except ValueError:
            continue
    return raw


def _normalize_event(trade_date: str, observed_at: str, event: dict) -> dict:
    symbol = _text(event.get("symbol"))
    code = _text(event.get("code")).zfill(6)
    event_time = _event_time(event.get("event_time", event.get("time", "")))
    info = _text(event.get("info"))
    key_source = "\x1f".join((trade_date, symbol, code, event_time, info))
    return {
        "trade_date": trade_date,
        "event_key": hashlib.sha256(key_source.encode("utf-8")).hexdigest(),
        "observed_at": observed_at,
        "event_time": event_time,
        "symbol": symbol,
        "code": code,
        "name": _text(event.get("name")),
        "sector_hint": _text(event.get("sector_hint")),
        "info": info,
    }


def insert_events(
    db_path: str | Path,
    trade_date: str,
    now: datetime,
    events: Iterable[dict],
    *,
    raw_dir: str | Path | None = None,
) -> list[dict]:
    """插入唯一事件，返回本次首次入库的记录。

    JSONL 只是首次入库事件的追加审计副本；SQLite 是下游消费的事实主库。
    """
    observed_at = now.isoformat(timespec="seconds")
    normalized = [_normalize_event(trade_date, observed_at, event) for event in events]
    inserted: list[dict] = []
    if not normalized:
        return inserted

    ensure_schema(db_path)
    with connect_close(db_path) as conn:
        for event in normalized:
            cur = conn.execute(
                """INSERT OR IGNORE INTO anomaly_event
                   (trade_date, event_key, observed_at, event_time, symbol, code,
                    name, sector_hint, info)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["trade_date"],
                    event["event_key"],
                    event["observed_at"],
                    event["event_time"],
                    event["symbol"],
                    event["code"],
                    event["name"],
                    event["sector_hint"],
                    event["info"],
                ),
            )
            if cur.rowcount != 1:
                continue
            event["id"] = int(cur.lastrowid)
            inserted.append(event)

    if raw_dir is not None and inserted:
        path = Path(raw_dir)
        path.mkdir(parents=True, exist_ok=True)
        raw_path = path / f"{trade_date.replace('-', '')}.jsonl"
        with raw_path.open("a", encoding="utf-8") as f:
            for event in inserted:
                record = {
                    "event_key": event["event_key"],
                    "round_ts": event["observed_at"],
                    "symbol": event["symbol"],
                    "code": event["code"],
                    "name": event["name"],
                    "time": event["event_time"],
                    "info": event["info"],
                    "sector_hint": event["sector_hint"],
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return inserted


def read_new_events(
    db_path: str | Path,
    consumer: str,
    trade_date: str,
    *,
    limit: int | None = None,
) -> list[dict]:
    """读取某消费者尚未确认的当日事件，不自动推进游标。"""
    ensure_schema(db_path)
    sql = """
        SELECT id, trade_date, event_key, observed_at, event_time, symbol, code,
               name, sector_hint, info
        FROM anomaly_event
        WHERE trade_date=?
          AND id > COALESCE(
              (SELECT last_event_id FROM anomaly_consumer_cursor
               WHERE consumer=? AND trade_date=?),
              0
          )
        ORDER BY id
    """
    params: list[object] = [trade_date, consumer, trade_date]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with connect_close(db_path) as conn:
        cur = conn.execute(sql, params)
        cols = [col[0] for col in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def advance_cursor(
    db_path: str | Path,
    consumer: str,
    trade_date: str,
    last_event_id: int,
) -> None:
    """确认已处理到指定事件 ID；游标只前进，不回退。"""
    ensure_schema(db_path)
    updated_at = datetime.now().isoformat(timespec="seconds")
    with connect_close(db_path) as conn:
        conn.execute(
            """INSERT INTO anomaly_consumer_cursor
               (consumer, trade_date, last_event_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(consumer, trade_date) DO UPDATE SET
                   last_event_id=MAX(anomaly_consumer_cursor.last_event_id, excluded.last_event_id),
                   updated_at=excluded.updated_at""",
            (consumer, trade_date, int(last_event_id), updated_at),
        )

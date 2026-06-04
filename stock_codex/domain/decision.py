from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from stock_codex.infra.db import connect_close as db_connect


LANES = {"main", "ambush", "backup", "trend", "ban"}
FACTIONS = {"A", "B", "C", "D", "E"}
ACTIONS = {"buy_if", "wait", "avoid", "sell", "empty"}
STATUSES = {"pending", "triggered", "bought", "expired", "invalid", "reviewed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    concept TEXT,
    lane TEXT NOT NULL CHECK(lane IN ('main','ambush','backup','trend','ban')),
    faction TEXT CHECK(faction IN ('A','B','C','D','E')),
    action TEXT NOT NULL DEFAULT 'wait' CHECK(action IN ('buy_if','wait','avoid','sell','empty')),
    entry_low REAL,
    entry_high REAL,
    max_chase_price REAL,
    stop_price REAL,
    target_pct REAL,
    invalid_price REAL,
    deadline_time TEXT,
    size_pct INTEGER,
    thesis TEXT,
    evidence_json TEXT,
    invalid_conditions_json TEXT,
    upgrade_conditions_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','triggered','bought','expired','invalid','reviewed')),
    source_msg_id INTEGER,
    origin TEXT,
    source_ref TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(trade_date, code, lane)
);
CREATE INDEX IF NOT EXISTS idx_decision_tickets_date ON decision_tickets(trade_date);
CREATE INDEX IF NOT EXISTS idx_decision_tickets_lane ON decision_tickets(trade_date, lane);
"""

LANE_RANK = {
    "main": 0,
    "ambush": 1,
    "backup": 2,
    "trend": 3,
    "ban": 4,
}

DECISION_BLOCK_RE = re.compile(
    r"```(?:decision_tickets|json\s+decision_tickets)\s*(\{.*?\})\s*```",
    re.DOTALL,
)


def ensure_schema(db: str | Path) -> None:
    with db_connect(db) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='decision_tickets'",
        ).fetchone()
        if row and row[0] and "'trend'" not in row[0]:
            conn.executescript("""
            ALTER TABLE decision_tickets RENAME TO decision_tickets_old;
            """)
        conn.executescript(SCHEMA)
        old_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_tickets_old'",
        ).fetchone()
        if old_exists:
            conn.execute(
                """INSERT OR IGNORE INTO decision_tickets
                   (id, trade_date, code, name, concept, lane, faction, action,
                    entry_low, entry_high, max_chase_price, stop_price, invalid_price,
                    deadline_time, size_pct, thesis, evidence_json,
                    invalid_conditions_json, upgrade_conditions_json, status, source_msg_id,
                    created_at, updated_at)
                   SELECT id, trade_date, code, name, concept, lane, faction, action,
                          entry_low, entry_high, max_chase_price, stop_price, invalid_price,
                          deadline_time, size_pct, thesis, evidence_json,
                          invalid_conditions_json, upgrade_conditions_json, status, source_msg_id,
                          created_at, updated_at
                   FROM decision_tickets_old""",
            )
            conn.execute("DROP TABLE decision_tickets_old")
            conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_decision_tickets_date ON decision_tickets(trade_date);
            CREATE INDEX IF NOT EXISTS idx_decision_tickets_lane ON decision_tickets(trade_date, lane);
            """)
        columns = {
            column[1] for column in conn.execute("PRAGMA table_info(decision_tickets)").fetchall()
        }
        if "target_pct" not in columns:
            conn.execute("ALTER TABLE decision_tickets ADD COLUMN target_pct REAL")
        if "origin" not in columns:
            conn.execute("ALTER TABLE decision_tickets ADD COLUMN origin TEXT")
        if "source_ref" not in columns:
            conn.execute("ALTER TABLE decision_tickets ADD COLUMN source_ref TEXT")


def _json_default(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def validate_tickets(tickets: list[dict[str, Any]]) -> None:
    main_count = sum(1 for t in tickets if t.get("lane") == "main")
    ambush_count = sum(1 for t in tickets if t.get("lane") == "ambush")
    if main_count > 1:
        raise ValueError("每天最多 1 只主攻票")
    if ambush_count > 2:
        raise ValueError("每天潜伏票最多 2 只")

    for t in tickets:
        lane = t.get("lane")
        if lane not in LANES:
            raise ValueError(f"unknown lane: {lane}")
        faction = t.get("faction")
        if faction is not None and faction not in FACTIONS:
            raise ValueError(f"unknown faction: {faction}")
        action = t.get("action", "wait")
        if action not in ACTIONS:
            raise ValueError(f"unknown action: {action}")
        status = t.get("status", "pending")
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        if not t.get("trade_date"):
            raise ValueError("trade_date is required")
        if not t.get("code"):
            raise ValueError("code is required")
        if not t.get("name"):
            raise ValueError("name is required")
        target_pct = t.get("target_pct")
        if target_pct is not None:
            try:
                target_pct_value = float(target_pct)
                if not math.isfinite(target_pct_value) or target_pct_value <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("target_pct must be a positive number") from None
        _validate_actionable_ticket(t)


def _missing_fields(t: dict[str, Any], fields: list[str]) -> list[str]:
    return [field for field in fields if t.get(field) is None]


def _validate_actionable_ticket(t: dict[str, Any]) -> None:
    lane = t.get("lane")
    if lane in {"main", "ambush"}:
        missing = _missing_fields(
            t,
            ["entry_low", "entry_high", "stop_price", "deadline_time", "size_pct"],
        )
        if lane == "main" and t.get("max_chase_price") is None:
            missing.append("max_chase_price")
        if missing:
            raise ValueError(f"{lane} 缺少可执行字段: {', '.join(missing)}")

    if lane in {"backup", "trend"}:
        missing = _missing_fields(
            t,
            ["entry_low", "entry_high", "max_chase_price", "stop_price", "deadline_time", "size_pct"],
        )
        if missing:
            raise ValueError(f"{lane} 缺少可执行字段: {', '.join(missing)}")


def parse_decision_block(text: str) -> tuple[str, list[dict[str, Any]]]:
    match = DECISION_BLOCK_RE.search(text)
    if not match:
        raise ValueError("missing ```decision_tickets fenced JSON block")
    payload = json.loads(match.group(1))
    trade_date = payload.get("trade_date")
    tickets = payload.get("tickets") or []
    if not trade_date:
        raise ValueError("decision_tickets.trade_date is required")
    if not isinstance(tickets, list):
        raise ValueError("decision_tickets.tickets must be a list")
    for t in tickets:
        t.setdefault("trade_date", trade_date)
    validate_tickets(tickets)
    return trade_date, tickets


def replace_tickets(db: str | Path, trade_date: str, tickets: list[dict[str, Any]]) -> int:
    validate_tickets(tickets)
    ensure_schema(db)
    with db_connect(db) as conn:
        conn.execute("DELETE FROM decision_tickets WHERE trade_date=?", (trade_date,))
        for t in tickets:
            conn.execute(
                """INSERT INTO decision_tickets
                   (trade_date, code, name, concept, lane, faction, action,
                    entry_low, entry_high, max_chase_price, stop_price, target_pct, invalid_price,
                    deadline_time, size_pct, thesis, evidence_json,
                    invalid_conditions_json, upgrade_conditions_json, status, source_msg_id,
                    origin, source_ref)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t["trade_date"], t["code"], t["name"], t.get("concept"),
                    t["lane"], t.get("faction"), t.get("action", "wait"),
                    t.get("entry_low"), t.get("entry_high"), t.get("max_chase_price"),
                    t.get("stop_price"), t.get("target_pct"), t.get("invalid_price"),
                    t.get("deadline_time"), t.get("size_pct"), t.get("thesis"),
                    _json_default(t.get("evidence", {})),
                    _json_default(t.get("invalid_conditions", [])),
                    _json_default(t.get("upgrade_conditions", [])),
                    t.get("status", "pending"), t.get("source_msg_id"),
                    t.get("origin", "premarket"), t.get("source_ref"),
                ),
            )
    return len(tickets)


def _row_to_ticket(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "trade_date": row[1],
        "code": row[2],
        "name": row[3],
        "concept": row[4],
        "lane": row[5],
        "faction": row[6],
        "action": row[7],
        "entry_low": row[8],
        "entry_high": row[9],
        "max_chase_price": row[10],
        "stop_price": row[11],
        "target_pct": row[12],
        "invalid_price": row[13],
        "deadline_time": row[14],
        "size_pct": row[15],
        "thesis": row[16],
        "evidence": _json_load(row[17], {}),
        "invalid_conditions": _json_load(row[18], []),
        "upgrade_conditions": _json_load(row[19], []),
        "status": row[20],
        "source_msg_id": row[21],
        "origin": row[22],
        "source_ref": row[23],
    }


def load_tickets(db: str | Path, trade_date: str) -> list[dict[str, Any]]:
    ensure_schema(db)
    with db_connect(db) as conn:
        rows = conn.execute(
            """SELECT id, trade_date, code, name, concept, lane, faction, action,
                      entry_low, entry_high, max_chase_price, stop_price, target_pct,
                      invalid_price, deadline_time, size_pct, thesis, evidence_json,
                      invalid_conditions_json, upgrade_conditions_json, status, source_msg_id,
                      origin, source_ref
               FROM decision_tickets
               WHERE trade_date=?
               ORDER BY
                 CASE lane
                   WHEN 'main' THEN 0
                   WHEN 'ambush' THEN 1
                   WHEN 'backup' THEN 2
                   WHEN 'trend' THEN 3
                   ELSE 4
                 END,
                 id""",
            (trade_date,),
        ).fetchall()
    return [_row_to_ticket(row) for row in rows]


def load_watchlist_compat(db: str | Path, trade_date: str) -> list[dict[str, Any]]:
    """Map decision tickets to the legacy watchlist shape used by L2 scripts."""
    out: list[dict[str, Any]] = []
    for t in load_tickets(db, trade_date):
        if t["lane"] not in {"main", "ambush", "backup", "trend"}:
            continue
        if t["status"] not in {"pending", "triggered"}:
            continue
        if t["lane"] == "ambush":
            buy = t.get("entry_low")
        else:
            buy = t.get("entry_high") or t.get("entry_low")
        out.append({
            "code": t["code"],
            "name": t["name"],
            "genre": t.get("faction") or "?",
            "lane": t["lane"],
            "buy": buy,
            "entry_low": t.get("entry_low"),
            "entry_high": t.get("entry_high"),
            "max_chase_price": t.get("max_chase_price"),
            "stop_loss": t.get("stop_price"),
            "target_pct": t.get("target_pct"),
            "deadline_time": t.get("deadline_time"),
            "position_max_pct": t.get("size_pct"),
            "status": t.get("status"),
            "thesis": t.get("thesis"),
        })
    return out


def mark_ticket_status(db: str | Path, trade_date: str, code: str, lane: str, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    ensure_schema(db)
    with db_connect(db) as conn:
        cur = conn.execute(
            """UPDATE decision_tickets
               SET status=?, updated_at=CURRENT_TIMESTAMP
               WHERE trade_date=? AND code=? AND lane=?""",
            (status, trade_date, code, lane),
        )
        return cur.rowcount > 0


def upsert_ticket(db: str | Path, ticket: dict[str, Any]) -> int | None:
    """插入或更新单张决策单；自动题材候选不得覆盖盘前或手工记录。"""
    validate_tickets([ticket])
    ensure_schema(db)
    incoming_origin = ticket.get("origin") or "premarket"
    with db_connect(db) as conn:
        existing = conn.execute(
            """SELECT id, origin FROM decision_tickets
               WHERE trade_date=? AND code=? AND lane=?""",
            (ticket["trade_date"], ticket["code"], ticket["lane"]),
        ).fetchone()
        if (
            existing
            and incoming_origin == "theme_candidate"
            and (existing[1] or "premarket") != "theme_candidate"
        ):
            return None
        conn.execute(
            """INSERT INTO decision_tickets
               (trade_date, code, name, concept, lane, faction, action,
                entry_low, entry_high, max_chase_price, stop_price, target_pct,
                invalid_price, deadline_time, size_pct, thesis, evidence_json,
                invalid_conditions_json, upgrade_conditions_json, status,
                source_msg_id, origin, source_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_date, code, lane) DO UPDATE SET
                   name=excluded.name,
                   concept=excluded.concept,
                   faction=excluded.faction,
                   action=excluded.action,
                   entry_low=excluded.entry_low,
                   entry_high=excluded.entry_high,
                   max_chase_price=excluded.max_chase_price,
                   stop_price=excluded.stop_price,
                   target_pct=excluded.target_pct,
                   invalid_price=excluded.invalid_price,
                   deadline_time=excluded.deadline_time,
                   size_pct=excluded.size_pct,
                   thesis=excluded.thesis,
                   evidence_json=excluded.evidence_json,
                   invalid_conditions_json=excluded.invalid_conditions_json,
                   upgrade_conditions_json=excluded.upgrade_conditions_json,
                   status=excluded.status,
                   source_msg_id=excluded.source_msg_id,
                   origin=excluded.origin,
                   source_ref=excluded.source_ref,
                   updated_at=CURRENT_TIMESTAMP
               WHERE excluded.origin != 'theme_candidate'
                  OR COALESCE(decision_tickets.origin, 'premarket') = 'theme_candidate'""",
            (
                ticket["trade_date"],
                ticket["code"],
                ticket["name"],
                ticket.get("concept"),
                ticket["lane"],
                ticket.get("faction"),
                ticket.get("action", "wait"),
                ticket.get("entry_low"),
                ticket.get("entry_high"),
                ticket.get("max_chase_price"),
                ticket.get("stop_price"),
                ticket.get("target_pct"),
                ticket.get("invalid_price"),
                ticket.get("deadline_time"),
                ticket.get("size_pct"),
                ticket.get("thesis"),
                _json_default(ticket.get("evidence", {})),
                _json_default(ticket.get("invalid_conditions", [])),
                _json_default(ticket.get("upgrade_conditions", [])),
                ticket.get("status", "pending"),
                ticket.get("source_msg_id"),
                incoming_origin,
                ticket.get("source_ref"),
            ),
        )
        row = conn.execute(
            """SELECT id, origin FROM decision_tickets
               WHERE trade_date=? AND code=? AND lane=?""",
            (ticket["trade_date"], ticket["code"], ticket["lane"]),
        ).fetchone()
        if not row:
            return None
        if incoming_origin == "theme_candidate" and (row[1] or "premarket") != "theme_candidate":
            return None
        return int(row[0])


def invalidate_tickets(
    db: str | Path,
    trade_date: str,
    *,
    origin: str | None = None,
    concept: str | None = None,
    codes: list[str] | None = None,
    source_ref: str | None = None,
    reason: str | None = None,
) -> int:
    """将匹配的未触发决策单标为 invalid。"""
    ensure_schema(db)
    where = ["trade_date=?", "status='pending'"]
    params: list[Any] = [trade_date]
    if origin is not None:
        where.append("origin=?")
        params.append(origin)
    if concept is not None:
        where.append("concept=?")
        params.append(concept)
    if source_ref is not None:
        where.append("source_ref=?")
        params.append(source_ref)
    if codes:
        where.append(f"code IN ({','.join('?' for _ in codes)})")
        params.extend(codes)

    with db_connect(db) as conn:
        rows = conn.execute(
            f"SELECT id, invalid_conditions_json FROM decision_tickets WHERE {' AND '.join(where)}",
            params,
        ).fetchall()
        for ticket_id, raw_conditions in rows:
            conditions = _json_load(raw_conditions, [])
            if reason and reason not in conditions:
                conditions.append(reason)
            conn.execute(
                """UPDATE decision_tickets
                   SET status='invalid', invalid_conditions_json=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (_json_default(conditions), ticket_id),
            )
    return len(rows)

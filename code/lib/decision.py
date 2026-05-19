from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from db import connect as db_connect


LANES = {"main", "ambush", "backup", "ban"}
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
    lane TEXT NOT NULL CHECK(lane IN ('main','ambush','backup','ban')),
    faction TEXT CHECK(faction IN ('A','B','C','D','E')),
    action TEXT NOT NULL DEFAULT 'wait' CHECK(action IN ('buy_if','wait','avoid','sell','empty')),
    entry_low REAL,
    entry_high REAL,
    max_chase_price REAL,
    stop_price REAL,
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
    "ban": 3,
}

DECISION_BLOCK_RE = re.compile(
    r"```(?:decision_tickets|json\s+decision_tickets)\s*(\{.*?\})\s*```",
    re.DOTALL,
)


def ensure_schema(db: str | Path) -> None:
    with db_connect(db) as conn:
        conn.executescript(SCHEMA)


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
                    entry_low, entry_high, max_chase_price, stop_price, invalid_price,
                    deadline_time, size_pct, thesis, evidence_json,
                    invalid_conditions_json, upgrade_conditions_json, status, source_msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t["trade_date"], t["code"], t["name"], t.get("concept"),
                    t["lane"], t.get("faction"), t.get("action", "wait"),
                    t.get("entry_low"), t.get("entry_high"), t.get("max_chase_price"),
                    t.get("stop_price"), t.get("invalid_price"),
                    t.get("deadline_time"), t.get("size_pct"), t.get("thesis"),
                    _json_default(t.get("evidence", {})),
                    _json_default(t.get("invalid_conditions", [])),
                    _json_default(t.get("upgrade_conditions", [])),
                    t.get("status", "pending"), t.get("source_msg_id"),
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
        "invalid_price": row[12],
        "deadline_time": row[13],
        "size_pct": row[14],
        "thesis": row[15],
        "evidence": _json_load(row[16], {}),
        "invalid_conditions": _json_load(row[17], []),
        "upgrade_conditions": _json_load(row[18], []),
        "status": row[19],
        "source_msg_id": row[20],
    }


def load_tickets(db: str | Path, trade_date: str) -> list[dict[str, Any]]:
    ensure_schema(db)
    with db_connect(db) as conn:
        rows = conn.execute(
            """SELECT id, trade_date, code, name, concept, lane, faction, action,
                      entry_low, entry_high, max_chase_price, stop_price, invalid_price,
                      deadline_time, size_pct, thesis, evidence_json,
                      invalid_conditions_json, upgrade_conditions_json, status, source_msg_id
               FROM decision_tickets
               WHERE trade_date=?
               ORDER BY
                 CASE lane
                   WHEN 'main' THEN 0
                   WHEN 'ambush' THEN 1
                   WHEN 'backup' THEN 2
                   ELSE 3
                 END,
                 id""",
            (trade_date,),
        ).fetchall()
    return [_row_to_ticket(row) for row in rows]


def load_watchlist_compat(db: str | Path, trade_date: str) -> list[dict[str, Any]]:
    """Map decision tickets to the legacy watchlist shape used by L2 scripts."""
    out: list[dict[str, Any]] = []
    for t in load_tickets(db, trade_date):
        if t["lane"] not in {"main", "ambush", "backup"}:
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
            "deadline_time": t.get("deadline_time"),
            "position_max_pct": t.get("size_pct"),
            "thesis": t.get("thesis"),
        })
    return out

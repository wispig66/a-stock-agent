"""stock-market-dynamic 盘面动态事实包。

用法:
  uv run .agents/skills/stock-market-dynamic/scripts/build_fact_pack.py --event-ids 1,2,3
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, time
from pathlib import Path
from typing import Any

from stock_codex.domain.decision import load_tickets
from stock_codex.domain.holdings import read_holdings


ROOT = Path(__file__).resolve().parents[4]
DB = ROOT / "data" / "daily.db"
OUT_FILE = ROOT / "data" / "allowed_latest_stock-market-dynamic.json"

CODE_RE = re.compile(r"\b(\d{6})\b")
ACTIVE_STATES = {"T1", "T2"}
ACTIVE_TICKET_STATUSES = {"pending", "triggered"}
STALE_AFTER_SECONDS = 10 * 60


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _parse_event_ids(raw: str) -> list[int]:
    out: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        event_id = int(item)
        if event_id not in out:
            out.append(event_id)
    if not out:
        raise ValueError("--event-ids 至少需要一个事件 ID")
    return out


def _fetch_events(conn: sqlite3.Connection, event_ids: list[int]) -> list[dict]:
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(
        f"""SELECT id, event_ts, trade_date, event_type, concept_tag, from_state,
                   to_state, score, payload_json
            FROM market_state_event
            WHERE id IN ({placeholders})
            ORDER BY event_ts, id""",
        event_ids,
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "event_ts": row["event_ts"],
            "trade_date": row["trade_date"],
            "event_type": row["event_type"],
            "concept_tag": row["concept_tag"],
            "from_state": row["from_state"],
            "to_state": row["to_state"],
            "score": float(row["score"]),
            "payload": _json_load(row["payload_json"], {}),
        }
        for row in rows
    ]


def _fetch_snapshots(
    conn: sqlite3.Connection,
    trade_date: str,
    as_of: datetime,
) -> tuple[dict, dict]:
    rows = conn.execute(
        """SELECT payload_json FROM market_snapshot
           WHERE trade_date=? AND snapshot_ts<=?
           ORDER BY snapshot_ts DESC LIMIT 2""",
        (trade_date, as_of.isoformat(timespec="seconds")),
    ).fetchall()
    current = _json_load(rows[0]["payload_json"], {}) if rows else {}
    previous = _json_load(rows[1]["payload_json"], {}) if len(rows) > 1 else {}
    for snapshot in (current, previous):
        if not snapshot:
            continue
        try:
            snapshot_at = datetime.fromisoformat(str(snapshot.get("snapshot_ts") or ""))
            if (as_of - snapshot_at).total_seconds() > STALE_AFTER_SECONDS:
                snapshot["is_stale"] = True
        except ValueError:
            snapshot["is_stale"] = True
    return current, previous


def _fetch_theme_states(conn: sqlite3.Connection, trade_date: str, as_of: datetime) -> list[dict]:
    rows = conn.execute(
        """SELECT s.concept_tag, s.state, s.score, s.components_json, s.primary_anchor,
                  s.evaluated_at, s.is_stale
           FROM theme_state_snapshot s
           JOIN (
               SELECT concept_tag, MAX(evaluated_at) AS evaluated_at
               FROM theme_state_snapshot
               WHERE trade_date=? AND evaluated_at<=?
               GROUP BY concept_tag
           ) latest
             ON latest.concept_tag=s.concept_tag AND latest.evaluated_at=s.evaluated_at
           WHERE s.trade_date=?
           ORDER BY s.score DESC, s.concept_tag""",
        (trade_date, as_of.isoformat(timespec="seconds"), trade_date),
    ).fetchall()
    return [
        {
            "theme": row["concept_tag"],
            "state": row["state"],
            "score": float(row["score"]),
            "components": _json_load(row["components_json"], {}),
            "primary_anchor": row["primary_anchor"],
            "evaluated_at": row["evaluated_at"],
            "is_stale": bool(row["is_stale"]),
        }
        for row in rows
    ]


def _holding_rows() -> list[dict]:
    try:
        holdings = read_holdings()
    except Exception:
        return []
    return [
        {
            "code": item.code,
            "name": item.name,
            "cost": item.cost,
            "shares": item.shares,
            "stop_loss": item.stop_loss,
            "take_profit": item.take_profit,
            "genre": item.genre,
        }
        for item in holdings
    ]


def _board_limit_pct(code: str) -> float:
    return 20.0 if code.startswith("30") else 10.0


def _is_near_limit(code: str, pct: float) -> bool:
    return pct >= _board_limit_pct(code) * 0.7


def _deadline_passed(deadline: str | None, as_of: datetime) -> bool:
    try:
        hour, minute = [int(part) for part in str(deadline or "").split(":")[:2]]
        return as_of.time() >= time(hour, minute)
    except (TypeError, ValueError):
        return True


def _actionable_candidates(
    tickets: list[dict],
    snapshot: dict,
    as_of: datetime | None = None,
) -> list[dict]:
    if snapshot.get("is_stale", True):
        return []
    as_of = as_of or datetime.now()
    stocks = snapshot.get("stocks") or {}
    out: list[dict] = []
    for ticket in tickets:
        if ticket.get("origin") != "theme_candidate":
            continue
        if ticket.get("lane") != "trend" or ticket.get("status") not in ACTIVE_TICKET_STATUSES:
            continue
        stock = stocks.get(ticket["code"]) or {}
        price = float(stock.get("price") or 0)
        pct = float(stock.get("pct") or 0)
        max_chase = float(ticket.get("max_chase_price") or 0)
        stop = float(ticket.get("stop_price") or 0)
        if (
            price <= 0
            or max_chase <= 0
            or stop <= 0
            or price > max_chase
            or price <= stop
            or _deadline_passed(ticket.get("deadline_time"), as_of)
            or _is_near_limit(ticket["code"], pct)
        ):
            continue
        out.append(ticket)
    return out


def _top_theme(theme_states: list[dict], snapshot: dict) -> str | None:
    active = [item for item in theme_states if item["state"] in ACTIVE_STATES]
    if active:
        return active[0]["theme"]
    strengths = snapshot.get("theme_strength") or {}
    if not strengths:
        return None
    return max(
        strengths,
        key=lambda theme: (
            float(strengths[theme].get("net_flow") or 0),
            float(strengths[theme].get("pct") or 0),
        ),
    )


def concentration_inference(
    events: list[dict],
    theme_states: list[dict],
    snapshot: dict,
    previous_snapshot: dict,
) -> tuple[bool, dict]:
    """只有资金增强、市场弱广度、其他题材降温同时成立时才允许集中度推断。"""
    if snapshot.get("is_stale", True):
        return False, {
            "top_theme": None,
            "current_net_flow": 0.0,
            "previous_net_flow": 0.0,
            "has_previous_flow": False,
            "flow_strengthened": False,
            "weak_breadth": False,
            "cooling_themes": [],
            "snapshot_stale": True,
        }
    top_theme = _top_theme(theme_states, snapshot)
    current_strength = (snapshot.get("theme_strength") or {}).get(top_theme or "") or {}
    previous_strengths = previous_snapshot.get("theme_strength") or {}
    previous_strength = previous_strengths.get(top_theme or "") or {}
    current_flow = float(current_strength.get("net_flow") or 0)
    previous_flow = float(previous_strength.get("net_flow") or 0)
    has_previous_flow = bool(
        top_theme
        and top_theme in previous_strengths
        and previous_strength.get("net_flow") is not None
    )
    flow_strengthened = has_previous_flow and current_flow > previous_flow

    breadth = snapshot.get("breadth") or {}
    weak_breadth = int(breadth.get("down") or 0) > int(breadth.get("up") or 0)
    cooling_themes = sorted({
        event["concept_tag"]
        for event in events
        if event["event_type"] in {"cooling", "rotation"} and event["concept_tag"] != top_theme
    })
    allowed = flow_strengthened and weak_breadth and bool(cooling_themes)
    return allowed, {
        "top_theme": top_theme,
        "current_net_flow": current_flow,
        "previous_net_flow": previous_flow,
        "has_previous_flow": has_previous_flow,
        "flow_strengthened": flow_strengthened,
        "weak_breadth": weak_breadth,
        "cooling_themes": cooling_themes,
        "snapshot_stale": False,
    }


def _codes_and_pct(
    snapshot: dict,
    tickets: list[dict],
    holdings: list[dict],
    theme_states: list[dict],
) -> tuple[dict[str, str], dict[str, float]]:
    stocks = snapshot.get("stocks") or {}
    anchors = snapshot.get("anchors") or {}
    name_to_code = {
        str(item.get("name") or ""): code
        for code, item in stocks.items()
        if item.get("name")
    }
    codes: dict[str, str] = {}

    for code, anchor in anchors.items():
        codes[code] = str(anchor.get("name") or stocks.get(code, {}).get("name") or "")
    for item in tickets + holdings:
        code = str(item.get("code") or "")
        if code:
            codes[code] = str(item.get("name") or stocks.get(code, {}).get("name") or "")
    for state in theme_states:
        anchor = str(state.get("primary_anchor") or "")
        match = CODE_RE.search(anchor)
        if match:
            code = match.group(1)
            codes[code] = str(stocks.get(code, {}).get("name") or anchor.replace(code, "").strip())
    for strength in (snapshot.get("theme_strength") or {}).values():
        leader = str(strength.get("leader") or "")
        code = name_to_code.get(leader)
        if code:
            codes[code] = leader

    pct = {
        code: round(float(stocks[code].get("pct") or 0), 2)
        for code in codes
        if code in stocks
    }
    return codes, pct


def build_fact_pack(
    db_path: str | Path,
    event_ids: list[int],
    *,
    as_of: datetime | None = None,
    holdings: list[dict] | None = None,
) -> dict:
    as_of = as_of or datetime.now()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        events = _fetch_events(conn, event_ids)
        if len(events) != len(event_ids):
            found = {event["id"] for event in events}
            missing = [event_id for event_id in event_ids if event_id not in found]
            raise ValueError(f"未找到 market_state_event: {missing}")
        trade_dates = {event["trade_date"] for event in events}
        if len(trade_dates) != 1:
            raise ValueError("一次盘面动态不能跨交易日合并事件")
        trade_date = next(iter(trade_dates))
        snapshot, previous_snapshot = _fetch_snapshots(conn, trade_date, as_of)
        theme_states = _fetch_theme_states(conn, trade_date, as_of)

    tickets = load_tickets(db_path, trade_date)
    holdings = holdings if holdings is not None else _holding_rows()
    candidates = _actionable_candidates(tickets, snapshot, as_of)
    concentration_allowed, concentration_evidence = concentration_inference(
        events,
        theme_states,
        snapshot,
        previous_snapshot,
    )
    codes, pct = _codes_and_pct(snapshot, tickets, holdings, theme_states)
    concepts = sorted({
        event["concept_tag"] for event in events
    } | {
        state["theme"] for state in theme_states
    } | {
        str(ticket.get("concept") or "") for ticket in tickets if ticket.get("concept")
    })

    return {
        "schema_version": "2",
        "skill": "stock-market-dynamic",
        "snapshot_at": snapshot.get("snapshot_ts") or as_of.isoformat(timespec="seconds"),
        "codes": codes,
        "lianban": {},
        "pct": pct,
        "summary": {
            "date": trade_date,
            "event_count": len(events),
            "snapshot_stale": bool(snapshot.get("is_stale", True)),
        },
        "concepts": concepts,
        "news": snapshot.get("news") or [],
        "global_markets": snapshot.get("overseas") or {},
        "market_breadth": snapshot.get("breadth") or {},
        "indices": snapshot.get("indices") or {},
        "turnover": snapshot.get("turnover") or {},
        "theme_strength": snapshot.get("theme_strength") or {},
        "overseas": snapshot.get("overseas") or {},
        "anchors": snapshot.get("anchors") or {},
        "pool_summary": snapshot.get("pool_summary") or {},
        "events": events,
        "theme_states": theme_states,
        "holdings": holdings,
        "decision_tickets": tickets,
        "actionable_candidates": candidates,
        "concentration_inference_allowed": concentration_allowed,
        "concentration_inference_evidence": concentration_evidence,
    }


def _print_rows(title: str, rows: list[dict], empty: str) -> None:
    print(f"\n## {title}\n")
    if not rows:
        print(f"- {empty}")
        return
    for row in rows:
        print(f"- {json.dumps(row, ensure_ascii=False, sort_keys=True)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-ids", required=True, help="逗号分隔的 market_state_event ID")
    parser.add_argument("--db", default=str(DB), help="SQLite daily.db 路径")
    args = parser.parse_args()

    try:
        event_ids = _parse_event_ids(args.event_ids)
        allowed = build_fact_pack(Path(args.db), event_ids)
    except (ValueError, sqlite3.Error) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    print(f"=== stock-market-dynamic fact pack · event_ids={','.join(map(str, event_ids))} ===")
    _print_rows("一、状态事件", allowed["events"], "无状态事件")
    _print_rows("二、题材状态", allowed["theme_states"], "无题材状态快照")
    _print_rows("三、锚点", list(allowed["anchors"].values()), "无锚点数据")
    _print_rows("四、持仓与票池", allowed["holdings"] + allowed["decision_tickets"], "无持仓或决策单")
    _print_rows("五、可执行候选", allowed["actionable_candidates"], "无可执行动态候选")
    print("\n=== ALLOWED ===")
    print(json.dumps(allowed, ensure_ascii=False, indent=2))
    print("=== /ALLOWED ===")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(allowed, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

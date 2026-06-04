"""题材五维评分、状态机和状态事件队列。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

from stock_codex.infra.db import connect_close
from stock_codex.paths import DB_FILE


T1_START = time(9, 35)
SHORT_COOLDOWN_SECONDS = 20 * 60
SHORT_DAILY_LIMIT = 8

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS theme_state_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluated_at TEXT NOT NULL,
    market_snapshot_ts TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    concept_tag TEXT NOT NULL,
    state TEXT NOT NULL,
    score REAL NOT NULL,
    components_json TEXT NOT NULL,
    consecutive_high INTEGER NOT NULL DEFAULT 0,
    consecutive_low INTEGER NOT NULL DEFAULT 0,
    is_stale INTEGER NOT NULL DEFAULT 0,
    primary_anchor TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(evaluated_at, concept_tag)
);
CREATE INDEX IF NOT EXISTS idx_theme_state_date_tag
    ON theme_state_snapshot(trade_date, concept_tag, evaluated_at);

CREATE TABLE IF NOT EXISTS market_state_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    concept_tag TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    score REAL NOT NULL,
    payload_json TEXT NOT NULL,
    queue_status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    processing_started_at TEXT,
    short_pushed_at TEXT,
    full_card_processed_at TEXT,
    error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_market_state_event_queue
    ON market_state_event(trade_date, queue_status, event_ts);
CREATE INDEX IF NOT EXISTS idx_market_state_event_theme
    ON market_state_event(trade_date, concept_tag, event_ts);
"""


def ensure_schema(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_close(path) as conn:
        conn.executescript(SCHEMA_SQL)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(market_state_event)").fetchall()
        }
        if "next_retry_at" not in columns:
            conn.execute("ALTER TABLE market_state_event ADD COLUMN next_retry_at TEXT")
        if "processing_started_at" not in columns:
            conn.execute("ALTER TABLE market_state_event ADD COLUMN processing_started_at TEXT")


@dataclass(frozen=True)
class ThemeEvaluation:
    theme: str
    state: str
    previous_state: str
    score: float
    components: dict[str, float]
    consecutive_high: int
    consecutive_low: int
    primary_anchor: str | None
    is_stale: bool


class ThemeSignal:
    def __init__(self, db_path: str | Path = DB_FILE):
        self.db_path = Path(db_path)
        ensure_schema(self.db_path)

    @staticmethod
    def _anomaly_score(events: list[dict]) -> float:
        unique = {str(event.get("event_key") or event.get("id") or idx) for idx, event in enumerate(events)}
        return float(min(30, len(unique) * 5))

    @staticmethod
    def _breadth_score(stats: dict) -> float:
        pct = max(0.0, float(stats.get("pct") or 0))
        strong = max(0, int(stats.get("strong_count") or 0))
        member_count = max(0, int(stats.get("member_count") or 0))
        up_count = max(0, int(stats.get("up_count") or 0))
        company_count = max(0, int(stats.get("company_count") or 0))
        net_flow = float(stats.get("net_flow") or 0)
        score = min(10.0, pct * 2)
        score += min(8.0, strong * 2)
        if member_count:
            score += min(5.0, up_count / member_count * 5)
        if net_flow > 0:
            score += 5.0
        if company_count > 0:
            score += 3.0
        return round(min(25.0, score), 2)

    @staticmethod
    def _anchor_score(theme: str, snapshot: dict) -> tuple[float, str | None]:
        candidates: list[tuple[float, str]] = []
        stats = (snapshot.get("theme_strength") or {}).get(theme) or {}
        if stats.get("leader_pct") is not None:
            candidates.append((
                float(stats.get("leader_pct") or 0),
                str(stats.get("leader") or theme),
            ))
        for code, anchor in (snapshot.get("anchors") or {}).items():
            if theme in (anchor.get("themes") or []):
                candidates.append((float(anchor.get("pct") or 0), f"{code} {anchor.get('name') or ''}".strip()))
        if not candidates:
            return 0.0, None
        pct, anchor = max(candidates)
        if pct >= 8:
            score = 20.0
        elif pct >= 5:
            score = 15.0
        elif pct >= 3:
            score = 10.0
        elif pct >= 1:
            score = 5.0
        else:
            score = 0.0
        return score, anchor

    @staticmethod
    def _catalyst_score(theme: str, snapshot: dict) -> float:
        news_hits = sum(1 for item in (snapshot.get("news") or []) if theme in (item.get("themes") or []))
        overseas_hits = [
            float(item.get("pct") or 0)
            for item in (snapshot.get("overseas") or {}).values()
            if theme in (item.get("themes") or []) and float(item.get("pct") or 0) > 0
        ]
        score = min(10.0, news_hits * 5.0)
        if overseas_hits:
            score += 5.0
        return min(15.0, score)

    @staticmethod
    def _confirmation_score(limit_up_count: int) -> float:
        return float(min(10, max(0, int(limit_up_count)) * 4))

    def _previous(self, trade_date: str, theme: str) -> dict | None:
        with connect_close(self.db_path) as conn:
            row = conn.execute(
                """SELECT market_snapshot_ts, state, score, consecutive_high, consecutive_low
                   FROM theme_state_snapshot
                   WHERE trade_date=? AND concept_tag=?
                   ORDER BY evaluated_at DESC LIMIT 1""",
                (trade_date, theme),
            ).fetchone()
        if not row:
            return None
        return {
            "market_snapshot_ts": row[0],
            "state": row[1],
            "score": float(row[2]),
            "consecutive_high": int(row[3]),
            "consecutive_low": int(row[4]),
        }

    @staticmethod
    def _base_state(
        previous_state: str,
        *,
        promotable: bool,
        t0: bool,
        t1: bool,
        t2: bool,
    ) -> str:
        if previous_state == "T2":
            return "T2"
        if previous_state == "T1":
            return "T2" if promotable and t2 else "T1"
        if not promotable:
            return previous_state if previous_state in {"T0", "COOLING", "ROTATED"} else "NONE"
        if t2:
            return "T2"
        if t1:
            return "T1"
        if t0:
            return "T0"
        return "NONE"

    @staticmethod
    def _event_type(state: str) -> str:
        return {
            "T0": "T0",
            "T1": "T1",
            "T2": "T2",
            "COOLING": "cooling",
            "ROTATED": "rotation",
        }[state]

    def evaluate(
        self,
        now: datetime,
        snapshot: dict,
        events_by_theme: dict[str, list[dict]],
        *,
        limit_up_counts: dict[str, int],
    ) -> tuple[list[ThemeEvaluation], list[dict]]:
        trade_date = now.strftime("%Y-%m-%d")
        snapshot_ts = str(snapshot.get("snapshot_ts") or now.isoformat(timespec="seconds"))
        strengths = snapshot.get("theme_strength") or {}
        themes = set(strengths) | set(events_by_theme)
        with connect_close(self.db_path) as conn:
            themes.update(
                row[0]
                for row in conn.execute(
                    """SELECT DISTINCT concept_tag FROM theme_state_snapshot
                       WHERE trade_date=? AND state IN ('T0','T1','T2')""",
                    (trade_date,),
                )
            )
        if not themes:
            return [], []

        scored: dict[str, dict] = {}
        for theme in themes:
            components = {
                "anomaly_flow": self._anomaly_score(events_by_theme.get(theme, [])),
                "breadth": self._breadth_score(strengths.get(theme) or {}),
                "catalyst": self._catalyst_score(theme, snapshot),
                "confirmation": self._confirmation_score(limit_up_counts.get(theme, 0)),
            }
            anchor_score, anchor = self._anchor_score(theme, snapshot)
            components["anchor"] = anchor_score
            score = round(sum(components.values()), 2)
            previous = self._previous(trade_date, theme) or {
                "market_snapshot_ts": "",
                "state": "NONE",
                "score": 0.0,
                "consecutive_high": 0,
                "consecutive_low": 0,
            }
            new_market_snapshot = previous["market_snapshot_ts"] != snapshot_ts
            high = previous["consecutive_high"]
            low = previous["consecutive_low"]
            if new_market_snapshot:
                high = high + 1 if score >= 70 else 0
                low = low + 1 if score < 45 else 0
            scored[theme] = {
                "components": components,
                "score": score,
                "anchor": anchor,
                "previous": previous,
                "high": high,
                "low": low,
            }

        leader_theme = max(scored, key=lambda theme: scored[theme]["score"])
        leader_score = scored[leader_theme]["score"]
        is_stale = bool(snapshot.get("is_stale"))
        evaluations: list[ThemeEvaluation] = []
        transitions: list[dict] = []
        evaluated_at = now.isoformat(timespec="seconds")

        with connect_close(self.db_path) as conn:
            for theme in sorted(themes):
                item = scored[theme]
                previous_state = item["previous"]["state"]
                components = item["components"]
                score = item["score"]
                t0 = score >= 30 and (components["catalyst"] > 0 or components["anomaly_flow"] > 0)
                t1 = (
                    score >= 55
                    and components["anomaly_flow"] >= 10
                    and (components["breadth"] >= 8 or components["anchor"] >= 8)
                    and now.time() >= T1_START
                )
                t2 = now.time() >= T1_START and (
                    score >= 80 or (score >= 70 and item["high"] >= 2)
                )
                state = self._base_state(
                    previous_state,
                    promotable=not is_stale,
                    t0=t0,
                    t1=t1,
                    t2=t2,
                )
                if previous_state in {"T1", "T2"}:
                    if theme != leader_theme and score < 55 and leader_score - score >= 15:
                        state = "ROTATED"
                    elif item["low"] >= 2:
                        state = "COOLING"

                evaluation = ThemeEvaluation(
                    theme=theme,
                    state=state,
                    previous_state=previous_state,
                    score=score,
                    components=components,
                    consecutive_high=item["high"],
                    consecutive_low=item["low"],
                    primary_anchor=item["anchor"],
                    is_stale=is_stale,
                )
                evaluations.append(evaluation)
                conn.execute(
                    """INSERT OR REPLACE INTO theme_state_snapshot
                       (evaluated_at, market_snapshot_ts, trade_date, concept_tag, state,
                        score, components_json, consecutive_high, consecutive_low,
                        is_stale, primary_anchor)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evaluated_at,
                        snapshot_ts,
                        trade_date,
                        theme,
                        state,
                        score,
                        json.dumps(components, ensure_ascii=False),
                        item["high"],
                        item["low"],
                        int(is_stale),
                        item["anchor"],
                    ),
                )
                if state == previous_state or state == "NONE":
                    continue
                event_type = self._event_type(state)
                payload = {
                    "theme": theme,
                    "event_type": event_type,
                    "from_state": previous_state,
                    "to_state": state,
                    "score": score,
                    "components": components,
                    "primary_anchor": item["anchor"],
                    "market_snapshot_ts": snapshot_ts,
                    "leader_theme": leader_theme,
                    "leader_score": leader_score,
                    "is_stale": is_stale,
                }
                cur = conn.execute(
                    """INSERT INTO market_state_event
                       (event_ts, trade_date, event_type, concept_tag, from_state,
                        to_state, score, payload_json, queue_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evaluated_at,
                        trade_date,
                        event_type,
                        theme,
                        previous_state,
                        state,
                        score,
                        json.dumps(payload, ensure_ascii=False),
                        "short_only" if event_type == "T0" else "pending",
                    ),
                )
                payload["id"] = int(cur.lastrowid)
                transitions.append(payload)
        return evaluations, transitions

    def can_push_short(self, event_id: int, now: datetime) -> bool:
        trade_date = now.strftime("%Y-%m-%d")
        with connect_close(self.db_path) as conn:
            row = conn.execute(
                """SELECT concept_tag, short_pushed_at FROM market_state_event
                   WHERE id=? AND trade_date=?""",
                (int(event_id), trade_date),
            ).fetchone()
            if not row or row[1]:
                return False
            pushed_count = conn.execute(
                """SELECT COUNT(*) FROM market_state_event
                   WHERE trade_date=? AND short_pushed_at IS NOT NULL""",
                (trade_date,),
            ).fetchone()[0]
            if pushed_count >= SHORT_DAILY_LIMIT:
                return False
            last = conn.execute(
                """SELECT short_pushed_at FROM market_state_event
                   WHERE trade_date=? AND concept_tag=? AND short_pushed_at IS NOT NULL
                   ORDER BY short_pushed_at DESC LIMIT 1""",
                (trade_date, row[0]),
            ).fetchone()
        if not last:
            return True
        return (now - datetime.fromisoformat(last[0])).total_seconds() >= SHORT_COOLDOWN_SECONDS

    def mark_short_pushed(self, event_id: int, now: datetime) -> None:
        with connect_close(self.db_path) as conn:
            conn.execute(
                "UPDATE market_state_event SET short_pushed_at=? WHERE id=?",
                (now.isoformat(timespec="seconds"), int(event_id)),
            )

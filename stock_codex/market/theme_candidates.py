"""题材状态到可交易趋势候选的转换。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time
from pathlib import Path

from stock_codex.domain import decision
from stock_codex.infra.db import connect_close
from stock_codex.market.theme_graph import ThemeGraph
from stock_codex.market.theme_signal import ensure_schema as ensure_signal_schema
from stock_codex.paths import DB_FILE


MIN_AVG_AMOUNT_20D = 200_000_000
MAX_DYNAMIC_TICKETS_PER_DAY = 3
CANDIDATE_DEADLINE = "14:00"


class ThemeCandidateEngine:
    def __init__(self, db_path: str | Path = DB_FILE, graph: ThemeGraph | None = None):
        self.db_path = Path(db_path)
        self.graph = graph or ThemeGraph(db_path=self.db_path)
        decision.ensure_schema(self.db_path)
        ensure_signal_schema(self.db_path)

    @staticmethod
    def _deadline_passed(deadline: str, now: datetime) -> bool:
        try:
            hour, minute = [int(part) for part in deadline.split(":")[:2]]
            return now.time() >= time(hour, minute)
        except (TypeError, ValueError):
            return True

    @staticmethod
    def _board_from_code(code: str) -> str:
        if code.startswith("30"):
            return "chinext"
        if code.startswith("68"):
            return "star"
        if code.startswith(("4", "8", "92")):
            return "bse"
        if code.startswith(("00", "60")):
            return "main"
        return "unknown"

    @staticmethod
    def _near_limit_threshold(board: str) -> float:
        return 14.0 if board == "chinext" else 7.0

    def _stock_basic(self, code: str, fallback_name: str) -> dict:
        with connect_close(self.db_path) as conn:
            row = conn.execute(
                "SELECT name, board, is_st FROM stock_basic WHERE code=?",
                (code,),
            ).fetchone()
        name = str((row or [fallback_name])[0] or fallback_name)
        board = str(row[1] or self._board_from_code(code)) if row else self._board_from_code(code)
        is_st = bool(row[2]) if row else ("ST" in name.upper())
        return {"name": name, "board": board, "is_st": is_st}

    def _kline(self, code: str) -> list[dict]:
        with connect_close(self.db_path) as conn:
            rows = conn.execute(
                """SELECT date, low, close, amount
                   FROM daily_kline
                   WHERE code=?
                   ORDER BY date DESC LIMIT 20""",
                (code,),
            ).fetchall()
        return [
            {"date": row[0], "low": float(row[1]), "close": float(row[2]), "amount": float(row[3] or 0)}
            for row in reversed(rows)
            if row[1] is not None and row[2] is not None
        ]

    def _is_limit_up_or_broken(self, trade_date: str, code: str) -> bool:
        with connect_close(self.db_path) as conn:
            limit_up = conn.execute(
                """SELECT 1 FROM intraday_limit_up_snapshot
                   WHERE trade_date=? AND code=? LIMIT 1""",
                (trade_date, code),
            ).fetchone()
            broken = conn.execute(
                """SELECT 1 FROM anomaly_event
                   WHERE trade_date=? AND code=? AND symbol='打开涨停板' LIMIT 1""",
                (trade_date, code),
            ).fetchone()
        return bool(limit_up or broken)

    def _recent_hot_pool(self, theme: str, now: datetime) -> dict[str, dict]:
        trade_date = now.strftime("%Y-%m-%d")
        with connect_close(self.db_path) as conn:
            dates = [
                row[0]
                for row in conn.execute(
                    """SELECT DISTINCT date FROM ths_hot_reason
                       WHERE date<? ORDER BY date DESC LIMIT 3""",
                    (trade_date,),
                ).fetchall()
            ]
            if not dates:
                return {}
            placeholders = ",".join("?" for _ in dates)
            rows = conn.execute(
                f"""SELECT date, code, name, reason FROM ths_hot_reason
                    WHERE date IN ({placeholders})""",
                dates,
            ).fetchall()
        out: dict[str, dict] = {}
        for reason_date, code, name, reason in rows:
            matches = self.graph.resolve(str(code), str(name or ""), "", str(reason or ""), now)
            if theme not in {match.theme for match in matches}:
                continue
            out[str(code).zfill(6)] = {
                "code": str(code).zfill(6),
                "name": str(name or ""),
                "role": "recent_hot",
                "evidence": f"ths_hot_reason:{reason_date}",
            }
        return out

    def _mapped_anomaly_pool(self, theme: str, now: datetime) -> dict[str, dict]:
        trade_date = now.strftime("%Y-%m-%d")
        with connect_close(self.db_path) as conn:
            rows = conn.execute(
                """SELECT code, name, sector_hint, info, event_key
                   FROM anomaly_event WHERE trade_date=? ORDER BY id""",
                (trade_date,),
            ).fetchall()
        out: dict[str, dict] = {}
        for code, name, sector_hint, info, event_key in rows:
            matches = self.graph.resolve(str(code), str(name or ""), str(sector_hint or ""), str(info or ""), now)
            if theme not in {match.theme for match in matches}:
                continue
            normalized = str(code).zfill(6)
            out[normalized] = {
                "code": normalized,
                "name": str(name or ""),
                "role": "mapped_event",
                "evidence": f"anomaly_event:{event_key}",
            }
        return out

    def _candidate_pool(self, theme: str, now: datetime) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for member in self.graph.member_records(theme, include_children=True):
            out[member["code"]] = {
                "code": member["code"],
                "name": "",
                "role": member["role"],
                "evidence": f"catalog:{member['theme']}",
            }
        for pool in (self._recent_hot_pool(theme, now), self._mapped_anomaly_pool(theme, now)):
            for code, item in pool.items():
                out.setdefault(code, item)
        return out

    def _existing_dynamic_codes(self, trade_date: str) -> set[str]:
        with connect_close(self.db_path) as conn:
            return {
                row[0]
                for row in conn.execute(
                    """SELECT code FROM decision_tickets
                       WHERE trade_date=? AND lane='trend' AND origin='theme_candidate'""",
                    (trade_date,),
                )
            }

    def _existing_candidate_types(self, trade_date: str, theme: str) -> set[str]:
        return {
            str((ticket.get("evidence") or {}).get("candidate_type") or "")
            for ticket in decision.load_tickets(self.db_path, trade_date)
            if ticket.get("origin") == "theme_candidate"
            and ticket.get("lane") == "trend"
            and ticket.get("concept") == theme
        }

    def _remaining_daily_slots(self, trade_date: str) -> int:
        with connect_close(self.db_path) as conn:
            count = conn.execute(
                """SELECT COUNT(*) FROM decision_tickets
                   WHERE trade_date=? AND lane='trend' AND origin='theme_candidate'""",
                (trade_date,),
            ).fetchone()[0]
        return max(0, MAX_DYNAMIC_TICKETS_PER_DAY - int(count))

    def _ticket_for(
        self,
        theme: str,
        item: dict,
        stock: dict,
        now: datetime,
        source_ref: str,
    ) -> dict | None:
        code = item["code"]
        basic = self._stock_basic(code, stock.get("name") or item.get("name") or "")
        if basic["board"] not in {"main", "chinext"} or basic["is_st"] or "ST" in basic["name"].upper():
            return None
        pct = float(stock.get("pct") or 0)
        if pct >= self._near_limit_threshold(basic["board"]):
            return None
        if self._is_limit_up_or_broken(now.strftime("%Y-%m-%d"), code):
            return None

        kline = self._kline(code)
        if len(kline) < 20:
            return None
        avg_amount = sum(row["amount"] for row in kline) / 20
        if avg_amount < MIN_AVG_AMOUNT_20D:
            return None

        closes = [row["close"] for row in kline]
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        low5 = min(row["low"] for row in kline[-5:])
        entry_low = round(0.995 * ma5, 2)
        entry_high = round(1.01 * ma5, 2)
        max_chase = round(1.02 * ma5, 2)
        stop = round(0.99 * max(ma10, low5), 2)
        price = float(stock.get("price") or 0)
        if price <= 0 or price > max_chase or price <= stop:
            return None
        risk_pct = (entry_high - stop) / entry_high * 100 if entry_high else 100
        if stop >= entry_low or risk_pct > 5:
            return None

        role = str(item.get("role") or "")
        candidate_type = "anchor_pullback" if role in {"anchor", "anchors"} else "low_level_follower"
        return {
            "trade_date": now.strftime("%Y-%m-%d"),
            "code": code,
            "name": basic["name"] or stock.get("name") or item.get("name") or code,
            "concept": theme,
            "lane": "trend",
            "faction": "D",
            "action": "buy_if",
            "entry_low": entry_low,
            "entry_high": entry_high,
            "max_chase_price": max_chase,
            "stop_price": stop,
            "target_pct": 3.0,
            "invalid_price": max_chase,
            "deadline_time": CANDIDATE_DEADLINE,
            "size_pct": 10,
            "thesis": f"{theme} · {'趋势锚点回踩' if candidate_type == 'anchor_pullback' else '未大涨低位补涨'}",
            "evidence": {
                "candidate_type": candidate_type,
                "pool_evidence": item.get("evidence"),
                "ma5": round(ma5, 4),
                "ma10": round(ma10, 4),
                "low5": round(low5, 4),
                "avg_amount_20d": round(avg_amount, 2),
                "snapshot_pct": pct,
            },
            "invalid_conditions": [
                f"价格超过 {max_chase}",
                f"跌破 {stop}",
                f"{CANDIDATE_DEADLINE} 后未触发",
                "题材降温或轮出",
            ],
            "upgrade_conditions": [],
            "status": "pending",
            "origin": "theme_candidate",
            "source_ref": source_ref,
            "candidate_type": candidate_type,
        }

    def build(
        self,
        theme: str,
        state: str,
        snapshot: dict,
        now: datetime,
        *,
        source_ref: str,
    ) -> list[dict]:
        """T1 才生成候选；T0 不生成，T2 不因确认追价新增。"""
        if state != "T1" or snapshot.get("is_stale") or self._deadline_passed(CANDIDATE_DEADLINE, now):
            return []
        strength = (snapshot.get("theme_strength") or {}).get(theme) or {}
        if strength.get("candidate_allowed") is False:
            return []
        trade_date = now.strftime("%Y-%m-%d")
        slots = self._remaining_daily_slots(trade_date)
        if slots <= 0:
            return []
        stocks = snapshot.get("stocks") or {}
        existing = self._existing_dynamic_codes(trade_date)
        existing_types = self._existing_candidate_types(trade_date, theme)
        valid: list[dict] = []
        for code, item in self._candidate_pool(theme, now).items():
            if code in existing or code not in stocks:
                continue
            ticket = self._ticket_for(theme, item, stocks[code], now, source_ref)
            if ticket:
                valid.append(ticket)

        anchors = []
        if "anchor_pullback" not in existing_types:
            anchors = sorted(
                (ticket for ticket in valid if ticket["candidate_type"] == "anchor_pullback"),
                key=lambda ticket: -float(ticket["evidence"]["snapshot_pct"]),
            )[:1]
        followers = []
        if "low_level_follower" not in existing_types:
            followers = sorted(
                (ticket for ticket in valid if ticket["candidate_type"] == "low_level_follower"),
                key=lambda ticket: (
                    float(ticket["evidence"]["snapshot_pct"]),
                    -float(ticket["evidence"]["avg_amount_20d"]),
                ),
            )[:1]
        return (anchors + followers)[:slots]

    def _record_candidate_event(self, event_type: str, ticket: dict, now: datetime, reason: str = "") -> int:
        payload = {
            "event_type": event_type,
            "theme": ticket["concept"],
            "code": ticket["code"],
            "name": ticket["name"],
            "reason": reason,
            "ticket": ticket,
        }
        with connect_close(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO market_state_event
                   (event_ts, trade_date, event_type, concept_tag, from_state,
                    to_state, score, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now.isoformat(timespec="seconds"),
                    now.strftime("%Y-%m-%d"),
                    event_type,
                    ticket["concept"],
                    "T1",
                    "T1",
                    0,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def write(self, tickets: list[dict], now: datetime) -> list[dict]:
        written: list[dict] = []
        for ticket in tickets:
            ticket_id = decision.upsert_ticket(self.db_path, ticket)
            if ticket_id is None:
                continue
            result = dict(ticket)
            result["id"] = ticket_id
            result["event_id"] = self._record_candidate_event("candidate_added", result, now)
            written.append(result)
        return written

    def invalidate(
        self,
        theme: str,
        snapshot: dict,
        now: datetime,
        *,
        reason: str | None = None,
    ) -> list[dict]:
        """失效未触发自动候选：题材降温/轮出、超过追价上限或过截止时间。"""
        trade_date = now.strftime("%Y-%m-%d")
        tickets = [
            ticket
            for ticket in decision.load_tickets(self.db_path, trade_date)
            if ticket["lane"] == "trend"
            and ticket.get("origin") == "theme_candidate"
            and ticket.get("concept") == theme
            and ticket.get("status") == "pending"
        ]
        invalidated: list[dict] = []
        for ticket in tickets:
            ticket_reason = reason
            stock = (snapshot.get("stocks") or {}).get(ticket["code"]) or {}
            if ticket_reason is None and float(stock.get("price") or 0) > float(ticket.get("max_chase_price") or 0):
                ticket_reason = "超过追价上限"
            if ticket_reason is None and self._deadline_passed(str(ticket.get("deadline_time") or ""), now):
                ticket_reason = "超过截止时间"
            if ticket_reason is None:
                continue
            count = decision.invalidate_tickets(
                self.db_path,
                trade_date,
                origin="theme_candidate",
                concept=theme,
                codes=[ticket["code"]],
                reason=ticket_reason,
            )
            if not count:
                continue
            result = dict(ticket)
            result["reason"] = ticket_reason
            result["event_id"] = self._record_candidate_event("candidate_invalid", result, now, ticket_reason)
            invalidated.append(result)
        return invalidated

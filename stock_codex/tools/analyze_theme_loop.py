"""题材发现 V2 无未来数据回放、校准与验收报告。

跑法：
  uv run python -m stock_codex.tools.analyze_theme_loop
  uv run python -m stock_codex.tools.analyze_theme_loop --days 5
  uv run python -m stock_codex.tools.analyze_theme_loop --date 2026-06-03
  uv run python -m stock_codex.tools.analyze_theme_loop --json

信号回放只使用事件发生时已可见的唯一异动、市场快照和涨停快照。
盘后 ths_hot_reason 只作为 ground truth 评分，不参与盘中事件归因。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from stock_codex.market.theme_graph import REASON_SPLIT, ThemeGraph
from stock_codex.market.theme_signal import ThemeSignal
from stock_codex.paths import DB_FILE


DB = DB_FILE
GROUND_TRUTH_THRESHOLD = 3
EVENT_WINDOW_MINUTES = 5
RELEVANT_SYMBOLS = {"火箭发射", "封涨停板", "打开涨停板", "60日新高"}
LEVELS = ("T0", "T1", "T2")
LEVEL_RANK = {"T0": 0, "T1": 1, "T2": 2}
REQUIRED_CANDIDATE_FIELDS = (
    "entry_low",
    "entry_high",
    "max_chase_price",
    "stop_price",
    "target_pct",
    "deadline_time",
    "size_pct",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=0, help="最近 N 天（0=全部）")
    parser.add_argument("--date", type=str, default=None, help="单日详情，格式 YYYY-MM-DD")
    parser.add_argument("--json", dest="json_output", action="store_true", help="只输出 JSON")
    return parser.parse_args()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _date_filter(column: str, days: int = 0, single_date: str | None = None) -> tuple[str, list[str]]:
    if single_date:
        datetime.fromisoformat(single_date)
        return f" AND {column}=?", [single_date]
    if days > 0:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return f" AND {column}>=?", [cutoff]
    return "", []


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def event_effective_time(event: dict) -> datetime:
    """异动自带时间有效时优先使用；缺失或异常时回退首次观测时间。"""
    observed = _parse_dt(event.get("observed_at"))
    if observed is None:
        raise ValueError(f"invalid observed_at: {event.get('observed_at')}")
    raw = str(event.get("event_time") or "").strip()
    if not raw:
        return observed
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt).time()
            candidate = datetime.combine(observed.date(), parsed)
            if candidate <= observed + timedelta(minutes=5):
                return candidate
        except ValueError:
            continue
    return observed


def load_market_snapshots(
    db_path: str | Path,
    *,
    days: int = 0,
    single_date: str | None = None,
) -> list[dict]:
    where, params = _date_filter("trade_date", days, single_date)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "market_snapshot"):
            return []
        rows = conn.execute(
            f"""SELECT snapshot_ts, trade_date, is_stale, payload_json
                FROM market_snapshot WHERE 1=1 {where}
                ORDER BY snapshot_ts""",
            params,
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        payload.setdefault("snapshot_ts", row["snapshot_ts"])
        payload.setdefault("trade_date", row["trade_date"])
        payload.setdefault("is_stale", bool(row["is_stale"]))
        out.append(payload)
    return out


def load_anomaly_events(
    db_path: str | Path,
    *,
    days: int = 0,
    single_date: str | None = None,
) -> list[dict]:
    where, params = _date_filter("trade_date", days, single_date)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "anomaly_event"):
            return []
        rows = conn.execute(
            f"""SELECT id, trade_date, event_key, observed_at, event_time, symbol,
                       code, name, sector_hint, info
                FROM anomaly_event WHERE 1=1 {where}
                ORDER BY observed_at, id""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def map_anomaly_events(events: list[dict], graph: ThemeGraph) -> dict:
    """按每条事件自己的可见时刻做归因，禁止读取盘后同日 reason。"""
    mapped_events: list[dict] = []
    first_event_times: dict[tuple[str, str], datetime] = {}
    relevant = [event for event in events if event.get("symbol") in RELEVANT_SYMBOLS]
    mapped_count = 0
    unknown_count = 0
    for event in relevant:
        effective_at = event_effective_time(event)
        matches = graph.resolve(
            str(event.get("code") or ""),
            str(event.get("name") or ""),
            str(event.get("sector_hint") or ""),
            str(event.get("info") or ""),
            effective_at,
        )
        themes = list(dict.fromkeys(match.theme for match in matches if not match.temporary))
        if themes:
            mapped_count += 1
        else:
            unknown_count += 1
        item = dict(event)
        item["themes"] = themes
        item["_effective_at"] = effective_at
        item["_observed_at"] = _parse_dt(event.get("observed_at")) or effective_at
        mapped_events.append(item)
        for theme in themes:
            key = (str(event["trade_date"]), theme)
            previous = first_event_times.get(key)
            if previous is None or effective_at < previous:
                first_event_times[key] = effective_at
    total = len(relevant)
    return {
        "events": mapped_events,
        "first_event_times": first_event_times,
        "relevant_events": total,
        "mapped_events": mapped_count,
        "unknown_events": unknown_count,
        "mapping_coverage_pct": _pct(mapped_count, total),
        "unknown_theme_rate_pct": _pct(unknown_count, total),
    }


def load_ground_truth(
    db_path: str | Path,
    graph: ThemeGraph,
    dates: set[str],
) -> dict[str, set[str]]:
    """盘后 reason 仅在此处转换为 ground truth，不进入事件归因。"""
    if not dates:
        return {}
    placeholders = ",".join("?" for _ in dates)
    with sqlite3.connect(db_path) as conn:
        if not _table_exists(conn, "ths_hot_reason"):
            return {}
        rows = conn.execute(
            f"""SELECT date, reason FROM ths_hot_reason
                WHERE date IN ({placeholders}) AND COALESCE(reason, '')!=''""",
            sorted(dates),
        ).fetchall()

    counters: dict[str, Counter] = defaultdict(Counter)
    for trade_date, reason in rows:
        for raw_tag in (item.strip() for item in REASON_SPLIT.split(str(reason)) if item.strip()):
            matches = graph.resolve("", "", raw_tag, raw_tag, f"{trade_date}T15:30:00")
            themes = [match.theme for match in matches if not match.temporary] or [raw_tag]
            for theme in dict.fromkeys(themes):
                counters[str(trade_date)][theme] += 1
    return {
        trade_date: {
            theme for theme, count in counts.items() if count >= GROUND_TRUTH_THRESHOLD
        }
        for trade_date, counts in counters.items()
    }


def _limit_up_counts_as_of(
    conn: sqlite3.Connection,
    graph: ThemeGraph,
    trade_date: str,
    snapshot_ts: str,
) -> dict[str, int]:
    if not _table_exists(conn, "intraday_limit_up_snapshot"):
        return {}
    latest = conn.execute(
        """SELECT MAX(snapshot_ts) FROM intraday_limit_up_snapshot
           WHERE trade_date=? AND snapshot_ts<=?""",
        (trade_date, snapshot_ts),
    ).fetchone()[0]
    if not latest:
        return {}
    rows = conn.execute(
        """SELECT concept_top1, COUNT(DISTINCT code)
           FROM intraday_limit_up_snapshot
           WHERE trade_date=? AND snapshot_ts=? AND concept_top1 IS NOT NULL
           GROUP BY concept_top1""",
        (trade_date, latest),
    ).fetchall()
    out: dict[str, int] = defaultdict(int)
    for concept, count in rows:
        matches = graph.resolve("", "", str(concept), str(concept), snapshot_ts)
        themes = [match.theme for match in matches if not match.temporary] or [str(concept)]
        for theme in dict.fromkeys(themes):
            out[theme] += int(count)
    return dict(out)


def _levels_for_state(state: str) -> tuple[str, ...]:
    if state not in LEVEL_RANK:
        return ()
    return LEVELS[: LEVEL_RANK[state] + 1]


def replay_signals(
    db_path: str | Path,
    graph: ThemeGraph,
    snapshots: list[dict],
    mapped_events: list[dict],
) -> tuple[dict[str, dict[tuple[str, str], datetime]], list[dict]]:
    """按快照和事件观测时点重放 ThemeSignal；每帧只读取当时可见事实。"""
    detections: dict[str, dict[tuple[str, str], datetime]] = {level: {} for level in LEVELS}
    transitions: list[dict] = []
    events_by_date: dict[str, list[dict]] = defaultdict(list)
    for event in mapped_events:
        events_by_date[str(event["trade_date"])].append(event)
    snapshots_by_date: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    for snapshot in snapshots:
        snapshot_ts = str(snapshot.get("snapshot_ts") or "")
        at = _parse_dt(snapshot_ts)
        if at is None:
            continue
        trade_date = str(snapshot.get("trade_date") or at.date().isoformat())
        snapshots_by_date[trade_date].append((at, snapshot))

    with tempfile.TemporaryDirectory(prefix="theme-replay-") as tmp_dir:
        signal = ThemeSignal(Path(tmp_dir) / "replay.db")
        with sqlite3.connect(db_path) as source:
            for trade_date in sorted(snapshots_by_date):
                dated_snapshots = sorted(snapshots_by_date[trade_date], key=lambda item: item[0])
                tick_times = {at for at, _ in dated_snapshots}
                tick_times.update(
                    event["_observed_at"]
                    for event in events_by_date.get(trade_date, [])
                )
                snapshot_idx = -1
                for now in sorted(tick_times):
                    while (
                        snapshot_idx + 1 < len(dated_snapshots)
                        and dated_snapshots[snapshot_idx + 1][0] <= now
                    ):
                        snapshot_idx += 1
                    if snapshot_idx < 0:
                        continue
                    snapshot = dated_snapshots[snapshot_idx][1]
                    cutoff = now - timedelta(minutes=EVENT_WINDOW_MINUTES)
                    events_by_theme: dict[str, list[dict]] = defaultdict(list)
                    for event in events_by_date.get(trade_date, []):
                        observed = event["_observed_at"]
                        if not (cutoff <= observed <= now):
                            continue
                        for theme in event["themes"]:
                            events_by_theme[theme].append(event)
                    limit_up_counts = _limit_up_counts_as_of(
                        source,
                        graph,
                        trade_date,
                        now.isoformat(timespec="seconds"),
                    )
                    evaluations, frame_transitions = signal.evaluate(
                        now,
                        snapshot,
                        dict(events_by_theme),
                        limit_up_counts=limit_up_counts,
                    )
                    transitions.extend(frame_transitions)
                    for evaluation in evaluations:
                        for level in _levels_for_state(evaluation.state):
                            detections[level].setdefault((trade_date, evaluation.theme), now)
    return detections, transitions


def load_recorded_state_detections(
    db_path: str | Path,
    *,
    days: int = 0,
    single_date: str | None = None,
) -> dict[str, dict[tuple[str, str], datetime]]:
    detections: dict[str, dict[tuple[str, str], datetime]] = {level: {} for level in LEVELS}
    where, params = _date_filter("trade_date", days, single_date)
    with sqlite3.connect(db_path) as conn:
        if not _table_exists(conn, "theme_state_snapshot"):
            return detections
        rows = conn.execute(
            f"""SELECT evaluated_at, trade_date, concept_tag, state
                FROM theme_state_snapshot WHERE 1=1 {where}
                ORDER BY evaluated_at""",
            params,
        ).fetchall()
    for evaluated_at, trade_date, theme, state in rows:
        at = _parse_dt(evaluated_at)
        if at is None:
            continue
        for level in _levels_for_state(str(state)):
            detections[level].setdefault((str(trade_date), str(theme)), at)
    return detections


def classification_metrics(
    detections: dict[tuple[str, str], datetime],
    ground_truth: dict[str, set[str]],
) -> dict:
    scored_dates = set(ground_truth)
    predicted = {key for key in detections if key[0] in scored_dates}
    actual = {
        (trade_date, theme)
        for trade_date, themes in ground_truth.items()
        for theme in themes
    }
    true_positive = predicted & actual
    precision = _ratio(len(true_positive), len(predicted))
    recall = _ratio(len(true_positive), len(actual))
    f1 = None
    if precision is not None and recall is not None:
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "scored_dates": len(scored_dates),
        "predicted": len(predicted),
        "actual": len(actual),
        "true_positive": len(true_positive),
        "false_positive": len(predicted - actual),
        "false_negative": len(actual - predicted),
        "precision_pct": _round_pct(precision),
        "recall_pct": _round_pct(recall),
        "f1_pct": _round_pct(f1),
    }


def discovery_delay_metrics(
    t1_detections: dict[tuple[str, str], datetime],
    first_event_times: dict[tuple[str, str], datetime],
) -> dict:
    delays: list[float] = []
    for key, detected_at in t1_detections.items():
        first_event = first_event_times.get(key)
        if first_event is None or first_event > detected_at:
            continue
        delays.append(round((detected_at - first_event).total_seconds() / 60, 2))
    return {
        "samples": len(delays),
        "t1_median_minutes": round(float(median(delays)), 2) if delays else None,
        "t1_average_minutes": round(sum(delays) / len(delays), 2) if delays else None,
        "t1_max_minutes": max(delays) if delays else None,
    }


def _snapshot_as_of(
    conn: sqlite3.Connection,
    trade_date: str,
    at: datetime,
) -> dict:
    if not _table_exists(conn, "market_snapshot"):
        return {}
    row = conn.execute(
        """SELECT payload_json FROM market_snapshot
           WHERE trade_date=? AND snapshot_ts<=?
           ORDER BY snapshot_ts DESC LIMIT 1""",
        (trade_date, at.isoformat(timespec="seconds")),
    ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {}


def _ticket_time(conn: sqlite3.Connection, ticket: dict) -> datetime | None:
    source_ref = str(ticket.get("source_ref") or "")
    match = re.fullmatch(r"market_state_event:(\d+)", source_ref)
    if match and _table_exists(conn, "market_state_event"):
        row = conn.execute(
            "SELECT event_ts FROM market_state_event WHERE id=?",
            (int(match.group(1)),),
        ).fetchone()
        if row:
            parsed = _parse_dt(row[0])
            if parsed:
                return parsed
    snapshot_match = re.fullmatch(r"theme_state_snapshot:(.+)", source_ref)
    if snapshot_match:
        parsed = _parse_dt(snapshot_match.group(1))
        if parsed:
            return parsed
    return _parse_dt(ticket.get("created_at"))


def _board_from_code(code: str) -> str:
    return "chinext" if code.startswith("30") else "main"


def _near_board_threshold(code: str) -> float:
    return 14.0 if _board_from_code(code) == "chinext" else 7.0


def _was_limit_up_as_of(
    conn: sqlite3.Connection,
    trade_date: str,
    code: str,
    at: datetime,
) -> bool:
    if not _table_exists(conn, "intraday_limit_up_snapshot"):
        return False
    return bool(
        conn.execute(
            """SELECT 1 FROM intraday_limit_up_snapshot
               WHERE trade_date=? AND code=? AND snapshot_ts<=? LIMIT 1""",
            (trade_date, code, at.isoformat(timespec="seconds")),
        ).fetchone()
    )


def _was_broken_as_of(
    conn: sqlite3.Connection,
    trade_date: str,
    code: str,
    at: datetime,
) -> bool:
    if not _table_exists(conn, "anomaly_event"):
        return False
    return bool(
        conn.execute(
            """SELECT 1 FROM anomaly_event
               WHERE trade_date=? AND code=? AND symbol='打开涨停板'
                 AND observed_at<=? LIMIT 1""",
            (trade_date, code, at.isoformat(timespec="seconds")),
        ).fetchone()
    )


def candidate_metrics(db_path: str | Path, dates: set[str]) -> dict:
    details: list[dict] = []
    if not dates:
        return _empty_candidate_metrics(details)
    placeholders = ",".join("?" for _ in dates)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "decision_tickets"):
            return _empty_candidate_metrics(details)
        rows = conn.execute(
            f"""SELECT id, trade_date, code, name, concept, entry_low, entry_high,
                       max_chase_price, stop_price, target_pct, deadline_time, size_pct,
                       status, origin, source_ref, created_at
                FROM decision_tickets
                WHERE origin='theme_candidate' AND lane='trend'
                  AND trade_date IN ({placeholders})
                ORDER BY trade_date, id""",
            sorted(dates),
        ).fetchall()
        for row in rows:
            ticket = dict(row)
            at = _ticket_time(conn, ticket)
            violations: list[str] = []
            missing = [field for field in REQUIRED_CANDIDATE_FIELDS if ticket.get(field) is None]
            if missing:
                violations.append("missing_fields")
            snapshot = _snapshot_as_of(conn, ticket["trade_date"], at) if at else {}
            stock = (snapshot.get("stocks") or {}).get(ticket["code"]) or {}
            price = float(stock.get("price") or 0)
            pct = float(stock.get("pct") or 0)
            if not stock:
                violations.append("missing_creation_snapshot")
            chase_violation = bool(
                price > 0
                and ticket.get("max_chase_price") is not None
                and price > float(ticket["max_chase_price"])
            )
            near_board = bool(stock and pct >= _near_board_threshold(ticket["code"]))
            limit_up = bool(at and _was_limit_up_as_of(conn, ticket["trade_date"], ticket["code"], at))
            broken = bool(at and _was_broken_as_of(conn, ticket["trade_date"], ticket["code"], at))
            if chase_violation:
                violations.append("over_max_chase")
            if near_board or limit_up:
                violations.append("near_or_on_board")
            if broken:
                violations.append("broken_board")
            entry_low = ticket.get("entry_low")
            entry_high = ticket.get("entry_high")
            stop = ticket.get("stop_price")
            if entry_low is not None and stop is not None and float(stop) >= float(entry_low):
                violations.append("invalid_stop")
            if entry_high and stop is not None:
                risk_pct = (float(entry_high) - float(stop)) / float(entry_high) * 100
                if risk_pct > 5:
                    violations.append("risk_over_5pct")
            details.append({
                "id": ticket["id"],
                "trade_date": ticket["trade_date"],
                "code": ticket["code"],
                "name": ticket["name"],
                "concept": ticket["concept"],
                "created_at": at.isoformat(timespec="seconds") if at else None,
                "complete": not missing,
                "executable": not violations,
                "chase_violation": chase_violation,
                "near_board": near_board or limit_up,
                "broken_board": broken,
                "violations": violations,
            })
    total = len(details)
    return {
        "total": total,
        "complete_count": sum(1 for item in details if item["complete"]),
        "complete_field_rate_pct": _pct(sum(1 for item in details if item["complete"]), total),
        "executable_count": sum(1 for item in details if item["executable"]),
        "executable_rate_pct": _pct(sum(1 for item in details if item["executable"]), total),
        "chase_violation_count": sum(1 for item in details if item["chase_violation"]),
        "near_board_count": sum(1 for item in details if item["near_board"]),
        "broken_board_count": sum(1 for item in details if item["broken_board"]),
        "details": details,
    }


def _empty_candidate_metrics(details: list[dict]) -> dict:
    return {
        "total": 0,
        "complete_count": 0,
        "complete_field_rate_pct": None,
        "executable_count": 0,
        "executable_rate_pct": None,
        "chase_violation_count": 0,
        "near_board_count": 0,
        "broken_board_count": 0,
        "details": details,
    }


def notification_metrics(db_path: str | Path, dates: set[str]) -> dict:
    if not dates:
        return {"short_alerts": 0, "full_cards": 0}
    placeholders = ",".join("?" for _ in dates)
    with sqlite3.connect(db_path) as conn:
        short_alerts = 0
        full_cards = 0
        if _table_exists(conn, "market_state_event"):
            short_alerts = int(
                conn.execute(
                    f"""SELECT COUNT(*) FROM market_state_event
                        WHERE trade_date IN ({placeholders}) AND short_pushed_at IS NOT NULL""",
                    sorted(dates),
                ).fetchone()[0]
            )
        if _table_exists(conn, "push_log"):
            full_cards = int(
                conn.execute(
                    f"""SELECT COUNT(*) FROM push_log
                        WHERE source='stock-market-dynamic' AND success=1
                          AND date(timestamp) IN ({placeholders})""",
                    sorted(dates),
                ).fetchone()[0]
            )
    return {"short_alerts": short_alerts, "full_cards": full_cards}


def state_replay_consistency(
    replay: dict[str, dict[tuple[str, str], datetime]],
    recorded: dict[str, dict[tuple[str, str], datetime]],
) -> dict:
    replay_pairs = {(level, *key) for level in LEVELS for key in replay[level]}
    recorded_pairs = {(level, *key) for level in LEVELS for key in recorded[level]}
    union = replay_pairs | recorded_pairs
    matches = union - (replay_pairs ^ recorded_pairs)
    return {
        "replay_state_count": len(replay_pairs),
        "recorded_state_count": len(recorded_pairs),
        "matching_state_count": len(matches),
        "consistency_pct": _pct(len(matches), len(union)),
        "drift_count": len(replay_pairs ^ recorded_pairs),
    }


def acceptance_checks(report: dict) -> dict:
    mapping = report["mapping"]
    metrics = report["classification"]
    delay = report["discovery_delay"]
    candidates = report["candidates"]
    checks = [
        _check(
            "相关异动映射覆盖率",
            mapping["mapping_coverage_pct"],
            ">=80%",
            mapping["relevant_events"] > 0
            and mapping["mapping_coverage_pct"] is not None
            and mapping["mapping_coverage_pct"] >= 80,
        ),
        _check(
            "T1 中位发现延迟",
            delay["t1_median_minutes"],
            "<=10min",
            delay["samples"] > 0
            and delay["t1_median_minutes"] is not None
            and delay["t1_median_minutes"] <= 10,
        ),
        _check(
            "T1 precision",
            metrics["T1"]["precision_pct"],
            ">=60%",
            metrics["T1"]["predicted"] > 0
            and metrics["T1"]["precision_pct"] is not None
            and metrics["T1"]["precision_pct"] >= 60,
        ),
        _check(
            "T2 precision",
            metrics["T2"]["precision_pct"],
            ">=80%",
            metrics["T2"]["predicted"] > 0
            and metrics["T2"]["precision_pct"] is not None
            and metrics["T2"]["precision_pct"] >= 80,
        ),
        _check(
            "自动候选完整字段率",
            candidates["complete_field_rate_pct"],
            "=100%",
            candidates["total"] > 0 and candidates["complete_field_rate_pct"] == 100,
        ),
        _check(
            "板上或近板候选数",
            candidates["near_board_count"],
            "=0",
            candidates["near_board_count"] == 0,
        ),
    ]
    return {"early_alert_ready": all(item["passed"] for item in checks), "checks": checks}


def _check(name: str, value: Any, target: str, passed: bool) -> dict:
    return {"name": name, "value": value, "target": target, "passed": bool(passed)}


def build_report(
    db_path: str | Path = DB,
    *,
    catalog_path: str | Path | None = None,
    days: int = 0,
    single_date: str | None = None,
) -> dict:
    graph = ThemeGraph(catalog_path, db_path=db_path) if catalog_path else ThemeGraph(db_path=db_path)
    snapshots = load_market_snapshots(db_path, days=days, single_date=single_date)
    events = load_anomaly_events(db_path, days=days, single_date=single_date)
    dates = {
        str(snapshot.get("trade_date") or "")
        for snapshot in snapshots
        if snapshot.get("trade_date")
    } | {
        str(event.get("trade_date") or "")
        for event in events
        if event.get("trade_date")
    }
    mapping = map_anomaly_events(events, graph)
    detections, transitions = replay_signals(db_path, graph, snapshots, mapping["events"])
    ground_truth = load_ground_truth(db_path, graph, dates)
    classification = {
        level: classification_metrics(detections[level], ground_truth)
        for level in LEVELS
    }
    recorded = load_recorded_state_detections(db_path, days=days, single_date=single_date)
    result = {
        "date_range": {
            "first": min(dates) if dates else None,
            "last": max(dates) if dates else None,
            "trade_dates": len(dates),
            "scored_dates": len(ground_truth),
        },
        "snapshot_count": len(snapshots),
        "mapping": {key: value for key, value in mapping.items() if key not in {"events", "first_event_times"}},
        "detections": detections,
        "transition_count": len(transitions),
        "ground_truth": ground_truth,
        "classification": classification,
        "discovery_delay": discovery_delay_metrics(detections["T1"], mapping["first_event_times"]),
        "state_replay": state_replay_consistency(detections, recorded),
        "candidates": candidate_metrics(db_path, dates),
        "notifications": notification_metrics(db_path, dates),
    }
    result["acceptance"] = acceptance_checks(result)
    return result


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _round_pct(value: float | None) -> float | None:
    return round(value * 100, 2) if value is not None else None


def _pct(numerator: int, denominator: int) -> float | None:
    return _round_pct(_ratio(numerator, denominator))


def _fmt(value: Any, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value}{suffix}"


def render_report(result: dict, *, single_date: str | None = None) -> None:
    date_range = result["date_range"]
    print(f"\n{'═' * 68}")
    print(
        "题材发现 V2 无未来回放 · "
        f"{date_range['first'] or '-'} → {date_range['last'] or '-'} "
        f"({date_range['trade_dates']} 个交易日)"
    )
    print(f"{'═' * 68}\n")

    if not result["snapshot_count"]:
        print("⚠️ 无 market_snapshot，无法完成状态回放。先运行新链路采集快照。\n")

    mapping = result["mapping"]
    print("🧭 异动归因")
    print(
        f"   相关唯一事件：{mapping['relevant_events']}  · 已映射：{mapping['mapped_events']}  "
        f"· 未知：{mapping['unknown_events']}"
    )
    print(
        f"   映射覆盖率：{_fmt(mapping['mapping_coverage_pct'], '%')}  "
        f"· 未知题材率：{_fmt(mapping['unknown_theme_rate_pct'], '%')}\n"
    )

    print(f"🎯 状态识别（已评分日期 {date_range['scored_dates']}/{date_range['trade_dates']}）")
    for level in LEVELS:
        metric = result["classification"][level]
        print(
            f"   {level}: precision {_fmt(metric['precision_pct'], '%')}  "
            f"recall {_fmt(metric['recall_pct'], '%')}  F1 {_fmt(metric['f1_pct'], '%')}  "
            f"(TP={metric['true_positive']} FP={metric['false_positive']} FN={metric['false_negative']})"
        )
    print()

    delay = result["discovery_delay"]
    print("⏱️ T1 发现延迟（首个已映射异动 → T1）")
    print(
        f"   样本 {delay['samples']}  · 中位 {_fmt(delay['t1_median_minutes'], 'min')}  "
        f"· 平均 {_fmt(delay['t1_average_minutes'], 'min')}  "
        f"· 最长 {_fmt(delay['t1_max_minutes'], 'min')}\n"
    )

    state = result["state_replay"]
    print("🔁 题材状态回放一致性")
    print(
        f"   回放状态 {state['replay_state_count']}  · 已落库状态 {state['recorded_state_count']}  "
        f"· 一致率 {_fmt(state['consistency_pct'], '%')}  · 漂移 {state['drift_count']}\n"
    )

    candidates = result["candidates"]
    print("🎫 自动候选")
    print(
        f"   总数 {candidates['total']}  · 完整字段率 {_fmt(candidates['complete_field_rate_pct'], '%')}  "
        f"· 可执行率 {_fmt(candidates['executable_rate_pct'], '%')}"
    )
    print(
        f"   追高违规 {candidates['chase_violation_count']}  · 板上/近板 {candidates['near_board_count']}  "
        f"· 炸板 {candidates['broken_board_count']}\n"
    )

    notifications = result["notifications"]
    print("🔔 通知数量")
    print(
        f"   即时短讯 {notifications['short_alerts']}  · 完整盘面动态 {notifications['full_cards']}\n"
    )

    acceptance = result["acceptance"]
    print("✅ 早期提醒默认启用验收")
    for check in acceptance["checks"]:
        mark = "通过" if check["passed"] else "未通过"
        print(f"   [{mark}] {check['name']}：{_fmt(check['value'])}（目标 {check['target']}）")
    print(
        f"\n   结论：{'可将 --push-level 调整为 all' if acceptance['early_alert_ready'] else '保持 --push-level=t2'}"
    )

    if single_date:
        print(f"\n📅 {single_date} 回放识别")
        for level in LEVELS:
            themes = sorted(theme for date, theme in result["detections"][level] if date == single_date)
            print(f"   {level}: {themes}")


def _json_ready(result: dict) -> dict:
    out = dict(result)
    out["detections"] = {
        level: [
            {"trade_date": date, "theme": theme, "detected_at": at.isoformat(timespec="seconds")}
            for (date, theme), at in sorted(items.items())
        ]
        for level, items in result["detections"].items()
    }
    out["ground_truth"] = {
        trade_date: sorted(themes)
        for trade_date, themes in result["ground_truth"].items()
    }
    return out


def report(args) -> None:
    result = build_report(DB, days=args.days, single_date=args.date)
    if getattr(args, "json_output", False):
        print(json.dumps(_json_ready(result), ensure_ascii=False, indent=2))
        return
    render_report(result, single_date=args.date)


if __name__ == "__main__":
    report(parse_args())

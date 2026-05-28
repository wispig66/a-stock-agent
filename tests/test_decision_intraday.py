from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "stock-intraday" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_realtime  # noqa: E402
import watch_loop  # noqa: E402
from stock_codex.domain import decision  # noqa: E402


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "daily.db"
    conn = sqlite3.connect(db)
    conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    conn.close()
    return db


def test_load_today_watchlist_prefers_decision_tickets(monkeypatch, tmp_path):
    db = make_db(tmp_path)
    decision.replace_tickets(db, date.today().isoformat(), [
        {
            "trade_date": date.today().isoformat(),
            "code": "600000",
            "name": "浦发银行",
            "lane": "main",
            "faction": "A",
            "entry_low": 10.0,
            "entry_high": 10.3,
            "max_chase_price": 10.4,
            "stop_price": 9.7,
            "deadline_time": "10:30",
            "size_pct": 20,
        },
    ])
    monkeypatch.setattr(fetch_realtime, "DB", db)

    watchlist = fetch_realtime.load_today_watchlist()

    assert watchlist == [{
        "code": "600000",
        "name": "浦发银行",
        "genre": "A",
        "lane": "main",
        "buy": 10.3,
        "entry_low": 10.0,
        "entry_high": 10.3,
        "max_chase_price": 10.4,
        "stop_loss": 9.7,
        "deadline_time": "10:30",
        "position_max_pct": 20,
        "status": "pending",
        "thesis": None,
    }]


def test_load_today_watchlist_merges_dynamic_trend_candidates(monkeypatch, tmp_path):
    db = make_db(tmp_path)
    today = date.today().isoformat()
    decision.replace_tickets(db, today, [
        {
            "trade_date": today,
            "code": "600000",
            "name": "浦发银行",
            "lane": "main",
            "faction": "A",
            "entry_low": 10.0,
            "entry_high": 10.3,
            "max_chase_price": 10.4,
            "stop_price": 9.7,
            "deadline_time": "10:30",
            "size_pct": 20,
        },
    ])
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO watchlist_dynamic
           (trade_date, created_at, concept_tag, code, name, role, entry_price,
            stop_price, target_pct, discipline_type, action_window, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (today, f"{today}T10:10:00", "AI算力", "000001", "平安银行", "follower",
         8.8, 8.5, 5.0, "D", "before_1030", "pending"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(fetch_realtime, "DB", db)

    watchlist = fetch_realtime.load_today_watchlist()

    assert [w["code"] for w in watchlist] == ["600000", "000001"]
    trend = watchlist[1]
    assert trend["lane"] == "trend"
    assert trend["buy"] == 8.8
    assert trend["max_chase_price"] == 9.02
    assert trend["deadline_time"] == "10:30"
    assert trend["source"] == "watchlist_dynamic"


def test_watch_loop_ambush_alerts_only_inside_low_absorb_zone():
    watch_map = {
        "000001": {
            "code": "000001",
            "name": "平安银行",
            "genre": "E",
            "lane": "ambush",
            "buy": 8.8,
            "entry_low": 8.8,
            "entry_high": 9.1,
            "stop_loss": 8.5,
            "deadline_time": "10:30",
            "position_max_pct": 10,
        },
    }

    inside = watch_loop.evaluate(
        {"代码": "000001", "名称": "平安银行", "最新价": 8.95, "涨跌幅": 1.2, "量比": 1.0},
        watch_map=watch_map,
        hold_map={},
        today=date(2026, 5, 19),
    )
    chased = watch_loop.evaluate(
        {"代码": "000001", "名称": "平安银行", "最新价": 9.3, "涨跌幅": 5.2, "量比": 1.5},
        watch_map=watch_map,
        hold_map={},
        today=date(2026, 5, 19),
    )

    assert any(kind == "ambush_zone" for kind, _ in inside)
    assert not any(kind in {"ambush_zone", "watch_trigger"} for kind, _ in chased)
    assert "✅ 可下单信号" in inside[0][1]
    assert "仓位" in inside[0][1]
    assert "止损" in inside[0][1]
    assert "截止" in inside[0][1]


def test_watch_loop_backup_waits_until_main_deadline_passes():
    watch_map = {
        "600000": {
            "code": "600000",
            "name": "主攻",
            "genre": "A",
            "lane": "main",
            "buy": 10.3,
            "entry_low": 10.0,
            "entry_high": 10.3,
            "max_chase_price": 10.4,
            "stop_loss": 9.7,
            "deadline_time": "10:30",
            "position_max_pct": 20,
        },
        "000001": {
            "code": "000001",
            "name": "备选",
            "genre": "A",
            "lane": "backup",
            "buy": 8.8,
            "entry_low": 8.6,
            "entry_high": 8.8,
            "max_chase_price": 9.0,
            "stop_loss": 8.4,
            "deadline_time": "10:30",
            "position_max_pct": 20,
        },
    }

    before = watch_loop.evaluate(
        {"代码": "000001", "名称": "备选", "最新价": 8.85, "涨跌幅": 3.0, "量比": 1.0},
        watch_map=watch_map,
        hold_map={},
        today=date(2026, 5, 19),
        now=watch_loop.datetime(2026, 5, 19, 10, 0),
    )
    after = watch_loop.evaluate(
        {"代码": "000001", "名称": "备选", "最新价": 8.85, "涨跌幅": 3.0, "量比": 1.0},
        watch_map=watch_map,
        hold_map={},
        today=date(2026, 5, 19),
        now=watch_loop.datetime(2026, 5, 19, 10, 31),
    )

    assert any(kind == "backup_wait" for kind, _ in before)
    assert not any(kind == "watch_trigger" for kind, _ in before)
    assert any(kind == "watch_trigger" for kind, _ in after)
    assert "✅ 可下单信号" in next(msg for kind, msg in after if kind == "watch_trigger")


def test_watch_loop_backup_does_not_trigger_after_main_already_triggered():
    watch_map = {
        "600000": {
            "code": "600000",
            "name": "主攻",
            "genre": "A",
            "lane": "main",
            "deadline_time": "10:30",
            "status": "triggered",
        },
        "000001": {
            "code": "000001",
            "name": "备选",
            "genre": "A",
            "lane": "backup",
            "buy": 8.8,
            "entry_low": 8.6,
            "entry_high": 8.8,
            "max_chase_price": 9.0,
            "stop_loss": 8.4,
            "deadline_time": "10:30",
            "position_max_pct": 20,
        },
    }

    alerts = watch_loop.evaluate(
        {"代码": "000001", "名称": "备选", "最新价": 8.85, "涨跌幅": 3.0, "量比": 1.0},
        watch_map=watch_map,
        hold_map={},
        today=date(2026, 5, 19),
        now=watch_loop.datetime(2026, 5, 19, 10, 45),
    )

    assert any(kind == "backup_wait" for kind, _ in alerts)
    assert not any(kind == "watch_trigger" for kind, _ in alerts)


def test_watch_loop_trend_alerts_inside_trend_buy_zone():
    watch_map = {
        "000001": {
            "code": "000001",
            "name": "趋势票",
            "genre": "D",
            "lane": "trend",
            "buy": 8.8,
            "entry_low": 8.8,
            "entry_high": 8.8,
            "max_chase_price": 9.02,
            "stop_loss": 8.5,
            "deadline_time": "10:30",
            "position_max_pct": 15,
        },
    }

    alerts = watch_loop.evaluate(
        {"代码": "000001", "名称": "趋势票", "最新价": 8.9, "涨跌幅": 4.0, "量比": 2.0},
        watch_map=watch_map,
        hold_map={},
        today=date(2026, 5, 19),
        now=watch_loop.datetime(2026, 5, 19, 10, 0),
    )

    assert any(kind == "watch_trigger" for kind, _ in alerts)
    assert "趋势买点" in next(msg for kind, msg in alerts if kind == "watch_trigger")
    assert "仓位 ≤15%" in next(msg for kind, msg in alerts if kind == "watch_trigger")

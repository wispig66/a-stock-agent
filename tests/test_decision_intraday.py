from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "stock-intraday" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "code"))

import fetch_realtime  # noqa: E402
import watch_loop  # noqa: E402
from lib import decision  # noqa: E402


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "daily.db"
    conn = sqlite3.connect(db)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
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
            "stop_price": 9.7,
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
        "max_chase_price": None,
        "stop_loss": 9.7,
        "deadline_time": None,
        "position_max_pct": 20,
        "thesis": None,
    }]


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

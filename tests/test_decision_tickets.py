from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

from stock_codex.domain import decision  # noqa: E402


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "daily.db"
    sqlite3.connect(db).executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    return db


def test_replace_and_load_decision_tickets_roundtrip_json_fields(tmp_path):
    db = make_db(tmp_path)
    tickets = [
        {
            "trade_date": "2026-05-19",
            "code": "600000",
            "name": "浦发银行",
            "concept": "银行",
            "lane": "main",
            "faction": "A",
            "action": "buy_if",
            "entry_low": 10.0,
            "entry_high": 10.3,
            "max_chase_price": 10.4,
            "stop_price": 9.7,
            "target_pct": 3.0,
            "deadline_time": "10:30",
            "size_pct": 20,
            "thesis": "板块启动，主攻一只",
            "evidence": {"limit_up_count": 3},
            "invalid_conditions": ["10:30 前不突破"],
            "upgrade_conditions": [],
            "source_msg_id": 101,
        },
        {
            "trade_date": "2026-05-19",
            "code": "000001",
            "name": "平安银行",
            "concept": "银行",
            "lane": "ambush",
            "faction": "E",
            "action": "buy_if",
            "entry_low": 8.8,
            "entry_high": 9.1,
            "stop_price": 8.5,
            "deadline_time": "2026-05-24",
            "size_pct": 10,
            "thesis": "事件预期埋伏，只低吸",
            "evidence": {"catalyst": "政策会议"},
            "invalid_conditions": ["催化落地无反应"],
            "upgrade_conditions": ["板块 3 只涨停"],
        },
    ]

    written = decision.replace_tickets(db, "2026-05-19", tickets)
    loaded = decision.load_tickets(db, "2026-05-19")

    assert written == 2
    assert [t["lane"] for t in loaded] == ["main", "ambush"]
    assert loaded[0]["evidence"]["limit_up_count"] == 3
    assert loaded[0]["target_pct"] == 3.0
    assert loaded[1]["invalid_conditions"] == ["催化落地无反应"]


def test_validate_tickets_rejects_multiple_main_and_oversized_ambush(tmp_path):
    db = make_db(tmp_path)
    base = {
        "trade_date": "2026-05-19",
        "code": "600000",
        "name": "浦发银行",
        "concept": "银行",
        "lane": "main",
        "faction": "A",
        "action": "buy_if",
    }

    with pytest.raises(ValueError, match="最多 1 只主攻"):
        decision.replace_tickets(db, "2026-05-19", [
            base,
            {**base, "code": "600001", "name": "另一只"},
        ])

    with pytest.raises(ValueError, match="潜伏"):
        decision.replace_tickets(db, "2026-05-19", [
            {**base, "lane": "ambush", "faction": "E", "code": f"00000{i}"}
            for i in range(3)
        ])


def test_validate_tickets_rejects_non_finite_target_pct(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(ValueError, match="target_pct"):
        decision.replace_tickets(db, "2026-05-19", [{
            "trade_date": "2026-05-19",
            "code": "600000",
            "name": "浦发银行",
            "lane": "ban",
            "action": "avoid",
            "target_pct": float("nan"),
        }])


def test_watchlist_compat_exposes_only_actionable_lanes(tmp_path):
    db = make_db(tmp_path)
    decision.replace_tickets(db, "2026-05-19", [
        {
            "trade_date": "2026-05-19",
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
        {
            "trade_date": "2026-05-19",
            "code": "000001",
            "name": "平安银行",
            "lane": "ambush",
            "faction": "E",
            "entry_low": 8.8,
            "entry_high": 9.1,
            "stop_price": 8.5,
            "deadline_time": "2026-05-24",
            "size_pct": 10,
        },
        {
            "trade_date": "2026-05-19",
            "code": "600002",
            "name": "禁买票",
            "lane": "ban",
            "faction": "D",
            "action": "avoid",
        },
        {
            "trade_date": "2026-05-19",
            "code": "600003",
            "name": "趋势票",
            "lane": "trend",
            "faction": "D",
            "entry_low": 12.0,
            "entry_high": 12.2,
            "max_chase_price": 12.5,
            "stop_price": 11.6,
            "deadline_time": "10:30",
            "size_pct": 15,
        },
    ])

    compat = decision.load_watchlist_compat(db, "2026-05-19")

    assert [w["code"] for w in compat] == ["600000", "000001", "600003"]
    assert compat[0]["buy"] == 10.3
    assert compat[0]["target_pct"] is None
    assert compat[0]["status"] == "pending"
    assert compat[1]["buy"] == 8.8
    assert compat[1]["entry_high"] == 9.1
    assert compat[2]["lane"] == "trend"
    assert compat[2]["buy"] == 12.2


def test_parse_decision_block_from_card():
    card = """
📋 盘前交易计划

```decision_tickets
{
  "trade_date": "2026-05-19",
  "tickets": [
    {
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
      "evidence": {"limit_up_count": 3}
    }
  ]
}
```
"""

    trade_date, tickets = decision.parse_decision_block(card)

    assert trade_date == "2026-05-19"
    assert tickets[0]["trade_date"] == "2026-05-19"
    assert tickets[0]["lane"] == "main"
    assert tickets[0]["evidence"] == {"limit_up_count": 3}


def test_validate_tickets_rejects_incomplete_actionable_tickets(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(ValueError, match="main 缺少可执行字段"):
        decision.replace_tickets(db, "2026-05-19", [
            {
                "trade_date": "2026-05-19",
                "code": "600000",
                "name": "浦发银行",
                "lane": "main",
                "faction": "A",
                "action": "buy_if",
                "entry_low": 10.0,
                "entry_high": 10.3,
                "stop_price": 9.7,
                "size_pct": 20,
            },
        ])

    with pytest.raises(ValueError, match="backup 缺少可执行字段"):
        decision.replace_tickets(db, "2026-05-19", [
            {
                "trade_date": "2026-05-19",
                "code": "000001",
                "name": "备选",
                "lane": "backup",
                "faction": "A",
            },
        ])

    with pytest.raises(ValueError, match="trend 缺少可执行字段"):
        decision.replace_tickets(db, "2026-05-19", [
            {
                "trade_date": "2026-05-19",
                "code": "000002",
                "name": "趋势",
                "lane": "trend",
                "faction": "D",
            },
        ])


def test_mark_ticket_status_updates_existing_ticket(tmp_path):
    db = make_db(tmp_path)
    decision.replace_tickets(db, "2026-05-19", [
        {
            "trade_date": "2026-05-19",
            "code": "000001",
            "name": "平安银行",
            "lane": "ambush",
            "faction": "E",
            "action": "buy_if",
            "entry_low": 8.8,
            "entry_high": 9.1,
            "stop_price": 8.5,
            "deadline_time": "2026-05-24",
            "size_pct": 10,
        },
    ])

    assert decision.mark_ticket_status(db, "2026-05-19", "000001", "ambush", "triggered") is True
    loaded = decision.load_tickets(db, "2026-05-19")

    assert loaded[0]["status"] == "triggered"


def test_ensure_schema_migrates_existing_decision_table_to_allow_trend(tmp_path):
    db = tmp_path / "daily.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE decision_tickets (
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
    """)
    conn.execute(
        """INSERT INTO decision_tickets
           (trade_date, code, name, lane, faction, action)
           VALUES ('2026-05-19', '600000', '旧票', 'ban', 'D', 'avoid')""",
    )
    conn.commit()
    conn.close()

    decision.ensure_schema(db)
    decision.replace_tickets(db, "2026-05-20", [
        {
            "trade_date": "2026-05-20",
            "code": "600003",
            "name": "趋势票",
            "lane": "trend",
            "faction": "D",
            "entry_low": 12.0,
            "entry_high": 12.2,
            "max_chase_price": 12.5,
            "stop_price": 11.6,
            "deadline_time": "10:30",
            "size_pct": 15,
        },
    ])

    assert decision.load_tickets(db, "2026-05-19")[0]["code"] == "600000"
    assert decision.load_tickets(db, "2026-05-20")[0]["lane"] == "trend"


def test_ensure_schema_adds_target_pct_to_existing_trend_table(tmp_path):
    db = make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE decision_tickets RENAME TO decision_tickets_with_target")
    schema_without_target = decision.SCHEMA.replace("    target_pct REAL,\n", "")
    conn.executescript(schema_without_target)
    conn.execute(
        """INSERT INTO decision_tickets
           (trade_date, code, name, lane, faction, action)
           VALUES ('2026-05-19', '600000', '旧票', 'ban', 'D', 'avoid')""",
    )
    conn.execute("DROP TABLE decision_tickets_with_target")
    conn.commit()
    conn.close()

    decision.ensure_schema(db)
    columns = {
        row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(decision_tickets)")
    }

    assert "target_pct" in columns
    assert decision.load_tickets(db, "2026-05-19")[0]["target_pct"] is None


def test_upsert_ticket_records_origin_and_does_not_overwrite_premarket_ticket(tmp_path):
    db = make_db(tmp_path)
    premarket = {
        "trade_date": "2026-05-19",
        "code": "600003",
        "name": "盘前趋势票",
        "concept": "CPO光模块",
        "lane": "trend",
        "faction": "D",
        "entry_low": 12.0,
        "entry_high": 12.2,
        "max_chase_price": 12.5,
        "stop_price": 11.6,
        "deadline_time": "10:30",
        "size_pct": 10,
        "origin": "premarket",
        "source_ref": "premarket:card",
    }
    decision.upsert_ticket(db, premarket)

    result = decision.upsert_ticket(db, {
        **premarket,
        "name": "自动候选不应覆盖",
        "entry_low": 13.0,
        "origin": "theme_candidate",
        "source_ref": "market_state_event:1",
    })
    loaded = decision.load_tickets(db, "2026-05-19")

    assert result is None
    assert loaded[0]["name"] == "盘前趋势票"
    assert loaded[0]["entry_low"] == 12.0
    assert loaded[0]["origin"] == "premarket"
    assert loaded[0]["source_ref"] == "premarket:card"


def test_theme_candidate_upsert_cannot_overwrite_concurrent_manual_ticket(tmp_path):
    db = make_db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TRIGGER inject_manual_before_theme_candidate
        BEFORE INSERT ON decision_tickets
        WHEN NEW.origin='theme_candidate'
        BEGIN
            INSERT OR IGNORE INTO decision_tickets
                (trade_date, code, name, lane, origin)
            VALUES
                (NEW.trade_date, NEW.code, '并发手工票', NEW.lane, 'manual');
        END;
        """)

    result = decision.upsert_ticket(db, {
        "trade_date": "2026-05-19",
        "code": "600003",
        "name": "自动候选",
        "concept": "CPO光模块",
        "lane": "trend",
        "faction": "D",
        "entry_low": 12.0,
        "entry_high": 12.2,
        "max_chase_price": 12.5,
        "stop_price": 11.6,
        "deadline_time": "14:00",
        "size_pct": 10,
        "origin": "theme_candidate",
        "source_ref": "market_state_event:1",
    })
    loaded = decision.load_tickets(db, "2026-05-19")

    assert result is None
    assert loaded[0]["name"] == "并发手工票"
    assert loaded[0]["origin"] == "manual"


def test_invalidate_tickets_only_invalidates_untriggered_matching_origin(tmp_path):
    db = make_db(tmp_path)
    base = {
        "trade_date": "2026-05-19",
        "name": "趋势票",
        "concept": "CPO光模块",
        "lane": "trend",
        "faction": "D",
        "entry_low": 12.0,
        "entry_high": 12.2,
        "max_chase_price": 12.5,
        "stop_price": 11.6,
        "deadline_time": "14:00",
        "size_pct": 10,
        "origin": "theme_candidate",
    }
    decision.upsert_ticket(db, {**base, "code": "600001", "source_ref": "event:1"})
    decision.upsert_ticket(db, {**base, "code": "600002", "source_ref": "event:2", "status": "triggered"})
    decision.upsert_ticket(db, {**base, "code": "600003", "source_ref": "manual", "origin": "premarket"})

    count = decision.invalidate_tickets(
        db,
        "2026-05-19",
        origin="theme_candidate",
        concept="CPO光模块",
        reason="题材降温",
    )
    loaded = {ticket["code"]: ticket for ticket in decision.load_tickets(db, "2026-05-19")}

    assert count == 1
    assert loaded["600001"]["status"] == "invalid"
    assert loaded["600002"]["status"] == "triggered"
    assert loaded["600003"]["status"] == "pending"

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from lib import decision  # noqa: E402


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "daily.db"
    sqlite3.connect(db).executescript((ROOT / "code" / "init_db.sql").read_text())
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
            "stop_price": 9.7,
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
    ])

    compat = decision.load_watchlist_compat(db, "2026-05-19")

    assert [w["code"] for w in compat] == ["600000", "000001"]
    assert compat[0]["buy"] == 10.3
    assert compat[1]["buy"] == 8.8
    assert compat[1]["entry_high"] == 9.1


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
      "stop_price": 9.7,
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

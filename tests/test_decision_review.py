from __future__ import annotations

import pandas as pd

from stock_codex.tools import review


def test_review_decision_tickets_scores_main_and_ambush():
    tickets = [
        {
            "code": "600000",
            "name": "浦发银行",
            "lane": "main",
            "faction": "A",
            "entry_high": 10.3,
            "stop_price": 9.7,
        },
        {
            "code": "000001",
            "name": "平安银行",
            "lane": "ambush",
            "faction": "E",
            "entry_low": 8.8,
            "entry_high": 9.1,
            "stop_price": 8.5,
        },
        {
            "code": "600002",
            "name": "禁买票",
            "lane": "ban",
            "faction": "D",
        },
    ]
    spot = pd.DataFrame([
        {"代码": "600000", "最高": 10.5, "最低": 10.0, "最新价": 10.4, "涨跌幅": 3.0},
        {"代码": "000001", "最高": 9.0, "最低": 8.7, "最新价": 8.95, "涨跌幅": 1.0},
        {"代码": "600002", "最高": 12.0, "最低": 10.0, "最新价": 11.8, "涨跌幅": 9.8},
    ])

    reviewed = review.review_decision_tickets(tickets, spot)

    assert reviewed[0]["status"] == "✅ 主攻触发+收红"
    assert reviewed[1]["status"] == "🟡 潜伏触达低吸区"
    assert reviewed[2]["status"] == "🚫 禁买后走强"

from __future__ import annotations

import json

import pandas as pd
import pytest

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


def test_review_decision_tickets_marks_incomplete_backup_unactionable():
    reviewed = review.review_decision_tickets(
        [{"code": "600000", "name": "浦发银行", "lane": "backup", "faction": "A"}],
        pd.DataFrame([
            {"代码": "600000", "最高": 10.5, "最低": 10.0, "最新价": 10.4, "涨跌幅": 3.0},
        ]),
    )

    assert reviewed[0]["status"] == "不可执行：缺少买点"


def write_split_cache(path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cached_at": "2026-05-25T15:35:00",
                "data": json.loads(df.to_json(orient="split", force_ascii=False)),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_decision_review_uses_postmarket_spot_cache_without_live_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(review, "WATCH_RAW_DIR", tmp_path / "watch_raw")
    monkeypatch.setattr(review, "ALLOWED_POSTMARKET", tmp_path / "allowed.json")
    write_split_cache(
        tmp_path / "postmarket_cache" / "20260525_spot.json",
        pd.DataFrame([
            {"代码": "600000", "名称": "浦发银行", "最高": 10.5, "最低": 9.9, "最新价": 10.4, "涨跌幅": 3.0},
        ]),
    )

    df = review.load_decision_review_spot("2026-05-25", ["600000"], "auto")

    assert df.to_dict("records")[0]["最高"] == 10.5
    assert df.attrs["review_source"] == "spot-cache:20260525_spot.json"


def test_decision_review_falls_back_to_watch_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(review, "WATCH_RAW_DIR", tmp_path / "watch_raw")
    monkeypatch.setattr(review, "ALLOWED_POSTMARKET", tmp_path / "allowed.json")
    raw = tmp_path / "watch_raw" / "20260525.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "\n".join([
            json.dumps({"round_ts": "2026-05-25T09:31:00", "代码": "600000", "名称": "浦发银行", "最新价": 10.1, "涨跌幅": 1.0, "最高": 10.2, "最低": 9.9}, ensure_ascii=False),
            json.dumps({"round_ts": "2026-05-25T14:57:00", "代码": "600000", "名称": "浦发银行", "最新价": 10.4, "涨跌幅": 3.0, "最高": 10.6, "最低": 9.8}, ensure_ascii=False),
        ]),
        encoding="utf-8",
    )

    df = review.load_decision_review_spot("2026-05-25", ["600000"], "auto")

    row = df.to_dict("records")[0]
    assert row["最高"] == 10.6
    assert row["最低"] == 9.8
    assert row["最新价"] == 10.4
    assert df.attrs["review_source"] == "watch-raw:20260525.jsonl"


def test_decision_review_scores_ban_with_partial_allowed_data(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(review, "WATCH_RAW_DIR", tmp_path / "watch_raw")
    allowed = tmp_path / "allowed.json"
    monkeypatch.setattr(review, "ALLOWED_POSTMARKET", allowed)
    allowed.write_text(
        json.dumps(
            {
                "summary": {"date": "2026-05-25"},
                "codes": {"600000": "浦发银行"},
                "pct": {"600000": 9.8},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    df = review.load_decision_review_spot("2026-05-25", ["600000"], "postmarket-partial")
    reviewed = review.review_decision_tickets(
        [{"code": "600000", "name": "浦发银行", "lane": "ban", "faction": "D"}],
        df,
    )

    assert reviewed[0]["status"] == "🚫 禁买后走强"
    assert "close" not in reviewed[0]


def test_decision_review_auto_stops_at_complete_partial_local_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(review, "WATCH_RAW_DIR", tmp_path / "watch_raw")
    allowed = tmp_path / "allowed.json"
    monkeypatch.setattr(review, "ALLOWED_POSTMARKET", allowed)
    monkeypatch.setattr(review, "_load_akshare_spot", lambda codes: pytest.fail("akshare should not be called"))
    allowed.write_text(
        json.dumps(
            {
                "summary": {"date": "2026-05-25"},
                "codes": {"600000": "浦发银行"},
                "pct": {"600000": 9.8},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    df = review.load_decision_review_spot("2026-05-25", ["600000"], "auto")

    assert df.attrs["review_source"] == "postmarket-structural-partial"
    assert df.to_dict("records")[0]["涨跌幅"] == 9.8


def test_decision_review_fails_closed_when_all_sources_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(review, "WATCH_RAW_DIR", tmp_path / "watch_raw")
    monkeypatch.setattr(review, "ALLOWED_POSTMARKET", tmp_path / "allowed.json")
    monkeypatch.setattr(review, "_load_akshare_spot", lambda codes: (_ for _ in ()).throw(OSError("network down")))

    with pytest.raises(review.ReviewDataUnavailable, match="network down"):
        review.load_decision_review_spot("2026-05-25", ["600000"], "auto")


def test_decision_review_ignores_allowed_file_without_matching_code(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "POSTMARKET_CACHE_DIR", tmp_path / "postmarket_cache")
    monkeypatch.setattr(review, "WATCH_RAW_DIR", tmp_path / "watch_raw")
    allowed = tmp_path / "allowed.json"
    monkeypatch.setattr(review, "ALLOWED_POSTMARKET", allowed)
    monkeypatch.setattr(review, "_load_akshare_spot", lambda codes: (_ for _ in ()).throw(OSError("network down")))
    allowed.write_text(
        json.dumps(
            {
                "summary": {"date": "2026-05-25"},
                "codes": {"000001": "平安银行"},
                "pct": {"000001": 1.2},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(review.ReviewDataUnavailable):
        review.load_decision_review_spot("2026-05-25", ["600000"], "auto")

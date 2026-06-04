"""theme_emergence_loop 核心逻辑 smoke test。

不打 akshare、不依赖本机 data/ 下的运行态文件。
"""
from __future__ import annotations
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent

from stock_codex.apps import theme_emergence_loop as theme_loop  # noqa: E402
from stock_codex.apps.theme_emergence_loop import (  # noqa: E402
    PageHinkley, Whitelist, map_to_concept, pick_candidates,
    load_whitelist, _norm_seal_time,
)
from stock_codex.tools import analyze_theme_loop  # noqa: E402


# ─── PageHinkley ───
def test_ph_detects_burst():
    """空窗 30 tick 后突然连续 10 tick 每 tick 5 个事件 → 应触发 drift。"""
    ph = PageHinkley(lamb=10, delta=0.05, min_samples=20)
    for _ in range(30):
        ph.update(0.0)
    assert not ph.drift_detected, "空窗期不应触发"
    triggered_at = None
    for i in range(10):
        ph.update(5.0)
        if ph.drift_detected and triggered_at is None:
            triggered_at = i
    assert triggered_at is not None
    assert triggered_at < 5


def test_ph_no_false_positive_on_noise():
    ph = PageHinkley(lamb=10, delta=0.05, min_samples=20)
    import random
    random.seed(42)
    triggered = False
    for _ in range(500):
        x = 1.0 if random.random() < 0.10 else 0.0
        ph.update(x)
        if ph.drift_detected:
            triggered = True
    assert not triggered


def test_ph_state_restore_uses_x_mean():
    """x_mean 必须恢复，否则 update 增量公式会误差大。"""
    ph1 = PageHinkley(lamb=10, delta=0.05, min_samples=20)
    for _ in range(100):
        ph1.update(2.0)
    s = ph1.snapshot()
    assert s["x_mean"] > 1.9  # 均值应接近 2

    ph2 = PageHinkley(lamb=10, delta=0.05, min_samples=20)
    ph2.restore(s)
    assert abs(ph2.x_mean - s["x_mean"]) < 1e-9


# ─── _norm_seal_time（P0-2 回归） ───
def test_norm_seal_time_hhmmss_format():
    assert _norm_seal_time("092500") == "09:25:00"
    assert _norm_seal_time("103015") == "10:30:15"


def test_norm_seal_time_already_normalized_passthrough():
    assert _norm_seal_time("09:25:00") == "09:25:00"


def test_norm_seal_time_empty():
    assert _norm_seal_time("") == ""
    assert _norm_seal_time(None) == ""


def test_norm_seal_time_datetime_object():
    """akshare 偶尔返回 datetime.time"""
    from datetime import time as dtime
    assert _norm_seal_time(dtime(9, 25, 0)) == "09:25:00"


# ─── Concept whitelist ───
def _load_sample_whitelist(tmp_path: Path, monkeypatch, *, init_ths_table: bool = True) -> Whitelist:
    whitelist = tmp_path / "concept_whitelist.yaml"
    whitelist.write_text(
        """
存储芯片:
  keywords: [存储芯片, HBM]
  members: [301666]
人形机器人:
  keywords: [人形机器人]
  members: []
""".lstrip(),
        encoding="utf-8",
    )
    db = tmp_path / "daily.db"
    if init_ths_table:
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE ths_hot_reason (date TEXT, code TEXT, reason TEXT)")
    monkeypatch.setattr(theme_loop, "WHITELIST", whitelist)
    monkeypatch.setattr(theme_loop, "DB", db)
    return load_whitelist()


def test_whitelist_member_first(tmp_path, monkeypatch):
    wl = _load_sample_whitelist(tmp_path, monkeypatch)
    assert map_to_concept("301666", "大普微", "", wl) == "存储芯片"


def test_whitelist_keyword_fallback_on_name(tmp_path, monkeypatch):
    wl = _load_sample_whitelist(tmp_path, monkeypatch)
    tag = map_to_concept("999999", "未知人形机器人公司", "", wl)
    assert tag == "人形机器人"


def test_whitelist_no_match_returns_none(tmp_path, monkeypatch):
    wl = _load_sample_whitelist(tmp_path, monkeypatch)
    assert map_to_concept("999999", "完全无关名字", "无关概念", wl) is None


def test_load_whitelist_missing_ths_table_does_not_log_exception(tmp_path, monkeypatch):
    sqlite3.connect(tmp_path / "daily.db").close()
    exceptions = []
    monkeypatch.setattr(theme_loop.log, "exception", lambda *args, **kwargs: exceptions.append(args))

    wl = _load_sample_whitelist(tmp_path, monkeypatch, init_ths_table=False)

    assert len(wl) == 2
    assert exceptions == []


def test_whitelist_uses_concept_cache():
    """code 不在 members 也不在 name 关键词，但在 concept_cache 里能匹配"""
    # 手工构造 cache
    wl = Whitelist(themes={"存储芯片": {"keywords": ["存储", "HBM"]}},
                   code_idx={}, kw_idx=[("存储", "存储芯片"), ("HBM", "存储芯片")],
                   concept_cache={"301379": "高带宽存储 HBM 概念"})
    # name 里没有关键词，但 cache 命中
    assert map_to_concept("301379", "天山电子", "", wl) == "存储芯片"


# ─── pick_candidates 决策树 ───
def test_pick_candidates_before_1030_leader_is_A_派():
    signals = {
        "first_leader": {"code": "301666", "name": "大普微",
                         "limit_up_count": 1, "open_count": 0},
        "members": [
            {"code": "301666", "name": "大普微",
             "limit_up_count": 1, "open_count": 0},
            {"code": "301308", "name": "江波龙",
             "limit_up_count": 1, "open_count": 0},
        ],
    }
    now = datetime(2026, 5, 18, 10, 15)
    cands = pick_candidates("2026-05-18", "存储芯片", signals, now)
    assert cands == [], "涨停池成员只能作为题材锚点，不能生成自动交易候选"


def test_pick_candidates_after_1400_returns_empty():
    """P2-4：≥14:00 不追新主线（追高风险大）"""
    signals = {
        "first_leader": {"code": "301666", "name": "大普微",
                         "limit_up_count": 1, "open_count": 0},
        "members": [
            {"code": "301666", "name": "大普微",
             "limit_up_count": 1, "open_count": 0},
            {"code": "301308", "name": "江波龙",
             "limit_up_count": 1, "open_count": 0},
        ],
    }
    now = datetime(2026, 5, 18, 14, 30)
    cands = pick_candidates("2026-05-18", "存储芯片", signals, now)
    assert cands == [], "after_1400 应返回空列表，不追新主线"


def test_pick_candidates_excludes_blown_followers():
    signals = {
        "first_leader": {"code": "301666", "name": "大普微",
                         "limit_up_count": 1, "open_count": 0},
        "members": [
            {"code": "301666", "name": "大普微",
             "limit_up_count": 1, "open_count": 0},
            {"code": "002074", "name": "国轩高科",
             "limit_up_count": 1, "open_count": 3},
        ],
    }
    now = datetime(2026, 5, 18, 10, 15)
    cands = pick_candidates("2026-05-18", "存储芯片", signals, now)
    codes = [c["code"] for c in cands]
    assert "002074" not in codes


def test_pick_candidates_never_returns_limit_up_members():
    signals = {
        "first_leader": {"code": "301666", "name": "大普微"},
        "members": [
            {"code": "301666", "name": "大普微", "open_count": 0},
            {"code": "301308", "name": "江波龙", "open_count": 0},
        ],
    }

    assert pick_candidates("2026-05-18", "存储芯片", signals, datetime(2026, 5, 18, 10, 15)) == []


def _sample_signals() -> dict:
    return {
        "PH": True,
        "cluster3": True,
        "cluster_count": 3,
        "first_seal_1030": True,
        "second_board": False,
        "first_leader": {"code": "301666", "name": "大普微"},
        "first_seal_time": "10:00:00",
        "members": [
            {"code": "301666", "name": "大普微", "first_seal_time": "10:00:00", "open_count": 0},
        ],
    }


def test_push_level_t2_suppresses_t1_and_pushes_t2(monkeypatch):
    sent = []
    monkeypatch.setattr(theme_loop, "PUSH_LEVEL", "t2")
    monkeypatch.setattr(theme_loop, "push_one", lambda text, source: sent.append((text, source)))
    now = datetime(2026, 5, 18, 10, 20)

    theme_loop.push_t1_card("存储芯片", _sample_signals(), now)
    assert sent == []

    theme_loop.push_t2_card(
        "存储芯片",
        _sample_signals(),
        [{"code": "301666", "name": "大普微", "discipline_type": "A", "action_window": "before_1030"}],
        now,
    )

    assert len(sent) == 1
    assert "主线确认" in sent[0][0]
    assert sent[0][1] == "theme-loop"


def test_push_t2_after_safe_window_is_observation_not_order_signal(monkeypatch):
    sent = []
    monkeypatch.setattr(theme_loop, "PUSH_LEVEL", "t2")
    monkeypatch.setattr(theme_loop, "push_one", lambda text, source: sent.append(text))

    theme_loop.push_t2_card("存储芯片", _sample_signals(), [], datetime(2026, 5, 18, 14, 30))

    assert len(sent) == 1
    assert "无盘中可下单候选" in sent[0]
    assert "✅ 可下单信号" not in sent[0]


def test_calibration_metrics_exclude_dates_without_ground_truth():
    detections = {
        ("2026-05-18", "存储芯片"): datetime(2026, 5, 18, 10, 0),
        ("2026-05-19", "机器人"): datetime(2026, 5, 19, 10, 0),
    }
    metrics = analyze_theme_loop.classification_metrics(
        detections,
        {"2026-05-18": {"存储芯片"}},
    )

    assert metrics["predicted"] == 1
    assert metrics["true_positive"] == 1
    assert metrics["false_positive"] == 0
    assert metrics["precision_pct"] == 100.0


def test_candidate_engine_builds_only_for_t1_evaluations_and_invalidates_on_cooling():
    calls = []

    class FakeCandidateEngine:
        def build(self, theme, state, snapshot, now, source_ref):
            calls.append(("build", theme, state, source_ref))
            return [{"code": "600000", "concept": theme}]

        def write(self, tickets, now):
            calls.append(("write", len(tickets)))
            return tickets

        def invalidate(self, theme, snapshot, now, reason=None):
            calls.append(("invalidate", theme, reason))
            return []

    transitions = [
        {"id": 1, "theme": "CPO光模块", "event_type": "T1"},
        {"id": 2, "theme": "AI硬件", "event_type": "T2"},
        {"id": 3, "theme": "电力", "event_type": "cooling"},
    ]
    evaluations = [
        SimpleNamespace(theme="CPO光模块", state="T1"),
        SimpleNamespace(theme="AI硬件", state="T2"),
    ]

    added, invalidated = theme_loop.handle_candidate_transitions(
        FakeCandidateEngine(),
        transitions,
        evaluations,
        {},
        datetime(2026, 6, 3, 10, 0),
    )

    assert added == [{"code": "600000", "concept": "CPO光模块"}]
    assert invalidated == []
    assert ("build", "CPO光模块", "T1", "market_state_event:1") in calls
    assert not any(call[:2] == ("build", "AI硬件") for call in calls)
    assert ("invalidate", "电力", "题材降温") in calls


def test_candidate_engine_retries_build_while_theme_remains_t1():
    calls = []

    class FakeCandidateEngine:
        def build(self, theme, state, snapshot, now, source_ref):
            calls.append(("build", theme, state, source_ref))
            return [{"code": "600000", "concept": theme}]

        def write(self, tickets, now):
            return tickets

        def invalidate(self, theme, snapshot, now, reason=None):
            return []

    now = datetime(2026, 6, 3, 10, 5)
    added, _ = theme_loop.handle_candidate_transitions(
        FakeCandidateEngine(),
        [],
        [SimpleNamespace(theme="CPO光模块", state="T1")],
        {},
        now,
    )

    assert added == [{"code": "600000", "concept": "CPO光模块"}]
    assert calls == [
        ("build", "CPO光模块", "T1", "theme_state_snapshot:2026-06-03T10:05:00"),
    ]


def test_main_tick_advances_anomaly_cursor_only_after_success(tmp_path, monkeypatch):
    db = tmp_path / "daily.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((ROOT / "stock_codex" / "schema" / "init_db.sql").read_text())
    graph = _load_sample_whitelist(tmp_path, monkeypatch, init_ths_table=False)
    monkeypatch.setattr(theme_loop, "DB", db)
    monkeypatch.setattr(theme_loop, "RAW_DIR", tmp_path / "anomaly_raw")
    monkeypatch.setattr(
        theme_loop,
        "fetch_anomaly_all",
        lambda now: [{
            "symbol": "火箭发射",
            "code": "301666",
            "name": "大普微",
            "event_time": "10:00:00",
            "info": "HBM",
            "sector_hint": "",
        }],
    )
    monkeypatch.setattr(theme_loop, "fetch_zt_pool", lambda today: [])

    class FailingSnapshot:
        def capture(self, now):
            raise RuntimeError("snapshot failed")

    state = {"consecutive_failures": 0, "snapshot_service": FailingSnapshot()}

    with pytest.raises(RuntimeError, match="snapshot failed"):
        theme_loop.main_tick(
            datetime(2026, 6, 3, 10, 0),
            "2026-06-03",
            graph,
            {},
            state,
        )

    pending = theme_loop.anomaly_events.read_new_events(db, "theme-loop", "2026-06-03")
    assert [event["code"] for event in pending] == ["301666"]

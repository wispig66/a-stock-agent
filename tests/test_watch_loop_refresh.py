"""watch_loop 监控目标动态刷新测试。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "stock-intraday" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import watch_loop  # noqa: E402

ANOMALY_PATH = ROOT / ".agents" / "skills" / "stock-anomaly" / "scripts" / "anomaly_loop.py"
SPEC = importlib.util.spec_from_file_location("anomaly_loop_refresh_test_module", ANOMALY_PATH)
anomaly_loop = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(anomaly_loop)


def test_load_monitor_targets_sees_holdings_added_after_start(monkeypatch):
    holdings = []
    monkeypatch.setattr(watch_loop, "load_today_watchlist", lambda: [{
        "code": "600519", "name": "贵州茅台",
    }])
    monkeypatch.setattr(watch_loop, "load_holdings", lambda: list(holdings))

    _, hold_map, codes = watch_loop.load_monitor_targets()
    assert hold_map == {}
    assert codes == ["600519"]

    holdings.append({"code": "002608", "name": "江苏国信", "cost": 10.253})
    _, hold_map, codes = watch_loop.load_monitor_targets()
    assert hold_map["002608"]["name"] == "江苏国信"
    assert codes == ["002608", "600519"]


def test_load_watched_codes_sees_holdings_added_after_start(monkeypatch):
    holdings = []
    monkeypatch.setattr(anomaly_loop, "load_today_watchlist", lambda: [{
        "code": "600519", "name": "贵州茅台",
    }])
    monkeypatch.setattr(anomaly_loop, "load_holdings", lambda: list(holdings))

    assert anomaly_loop.load_watched_codes() == {"600519"}

    holdings.append({"code": "002608", "name": "江苏国信", "cost": 10.253})
    assert anomaly_loop.load_watched_codes() == {"600519", "002608"}


def test_load_monitor_targets_keeps_watchlist_when_holdings_reload_fails(monkeypatch):
    monkeypatch.setattr(watch_loop.log, "exception", lambda *args, **kwargs: None)
    monkeypatch.setattr(watch_loop, "load_today_watchlist", lambda: [{
        "code": "600519", "name": "贵州茅台",
    }])
    monkeypatch.setattr(
        watch_loop, "load_holdings",
        lambda: (_ for _ in ()).throw(ValueError("bad holdings yaml")),
    )

    watch_map, hold_map, codes = watch_loop.load_monitor_targets()

    assert watch_map["600519"]["name"] == "贵州茅台"
    assert hold_map == {}
    assert codes == ["600519"]


def test_load_watched_codes_keeps_holdings_when_watchlist_reload_fails(monkeypatch):
    monkeypatch.setattr(anomaly_loop.log, "exception", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        anomaly_loop, "load_today_watchlist",
        lambda: (_ for _ in ()).throw(RuntimeError("db busy")),
    )
    monkeypatch.setattr(anomaly_loop, "load_holdings", lambda: [{
        "code": "002608", "name": "江苏国信",
    }])

    assert anomaly_loop.load_watched_codes() == {"002608"}

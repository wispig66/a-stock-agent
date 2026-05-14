"""query.py 联网函数测试：全部 mock requests.get/post。"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from lib import query  # noqa: E402


def _mock_response(json_data=None, text=None, status=200):
    m = MagicMock()
    m.status_code = status
    m.raise_for_status = lambda: None
    m.json.return_value = json_data
    m.text = text or ""
    return m


def test_fetch_realtime_sina():
    sample = ('var hq_str_sh600519="贵州茅台,1600.00,1599.50,1605.00,'
              '1610.00,1590.00,1605.00,1605.50,1000000,'
              '1600000000.00,placeholder,p,p,p,p,p,p,p,p,p,p,p,p,p,p,p,p,p,p,'
              '2026-05-14,15:00:00,00";')
    with patch("requests.get", return_value=_mock_response(text=sample)):
        r = query.fetch_realtime("600519")
    assert r["name"] == "贵州茅台"
    assert r["open"] == 1600.0
    assert r["close"] == 1605.0
    assert r["high"] == 1610.0
    assert r["low"] == 1590.0


def test_fetch_kline_returns_rows():
    rows = [{"day": f"2026-04-{i:02d}", "open": "1.0", "high": "1.1",
             "low": "0.9", "close": "1.05", "volume": "10000"}
            for i in range(1, 31)]
    with patch("requests.get", return_value=_mock_response(json_data=rows)):
        df = query.fetch_kline("600519", days=30)
    assert len(df) == 30
    assert set(df.columns) >= {"date", "open", "high", "low", "close", "vol"}


def test_fetch_concept_strength_smoke():
    with patch("requests.get", return_value=_mock_response(json_data={
        "data": {"diff": [{"f12": "BK0475", "f14": "白酒",
                           "f3": 1.2, "f104": "贵州茅台"}]}})):
        r = query.fetch_concept_strength("600519")
    assert "concept_name" in r
    assert "top_concepts" in r


def test_fetch_money_flow_5d():
    with patch("requests.get", return_value=_mock_response(json_data={
        "data": {"klines": [
            "2026-05-10,1.0e8,2e7,3e7,4e7,5e7,1.5e8,1605,0.5",
            "2026-05-11,-1.0e7,2e7,3e7,4e7,5e7,1.5e8,1605,0.5",
            "2026-05-12,2.0e7,2e7,3e7,4e7,5e7,1.5e8,1605,0.5",
            "2026-05-13,-5.0e6,2e7,3e7,4e7,5e7,1.5e8,1605,0.5",
            "2026-05-14,1.0e7,2e7,3e7,4e7,5e7,1.5e8,1605,0.5"]}})):
        df = query.fetch_money_flow("600519", days=5)
    assert len(df) == 5
    assert "main_in" in df.columns


def test_fetch_recent_news_returns_list():
    with patch("requests.get", return_value=_mock_response(json_data={
        "data": {"list": [{"title": "茅台分红方案公告",
                           "url": "https://x", "ctime": "2026-05-13 09:00"}]}})):
        items = query.fetch_recent_news("600519", days=7)
    assert isinstance(items, list)
    assert items and "title" in items[0] and "url" in items[0]


def test_fetch_recent_news_failure_returns_empty():
    with patch("requests.get", side_effect=Exception("net")):
        items = query.fetch_recent_news("600519", days=7)
    assert items == []

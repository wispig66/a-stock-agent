"""连亏心态提醒模块单元测试。"""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from lib import loss_streak  # noqa: E402


def test_compute_daily_pnl_empty():
    """空持仓 → pnl_pct=0。"""
    result = loss_streak.compute_daily_pnl(
        holdings=[], price_fn=lambda c: None, total_capital=500000
    )
    assert result == 0.0


def test_compute_daily_pnl_with_realtime_price():
    """实时价可取时算正确浮盈亏%。"""
    holdings = [
        {"code": "601991", "cost": 6.77, "shares": 1000},
        {"code": "600519", "cost": 1500.0, "shares": 100},
    ]
    prices = {"601991": 7.0, "600519": 1500.0}
    result = loss_streak.compute_daily_pnl(
        holdings, price_fn=lambda c: prices.get(c), total_capital=500000
    )
    # (7.0-6.77)*1000 + (1500-1500)*100 = 230 + 0 = 230
    # 230 / 500000 * 100 = 0.046%
    assert result == pytest.approx(0.046, abs=0.01)


def test_compute_daily_pnl_price_fallback_to_cost():
    """实时价拿不到时用 cost 兜底（pnl_pct=0）。"""
    holdings = [{"code": "601991", "cost": 6.77, "shares": 1000}]
    result = loss_streak.compute_daily_pnl(
        holdings, price_fn=lambda c: None, total_capital=500000
    )
    assert result == 0.0


def test_compute_daily_pnl_loss_scenario():
    """跌价情景 → 负 pnl_pct。"""
    holdings = [{"code": "601991", "cost": 6.77, "shares": 10000}]
    result = loss_streak.compute_daily_pnl(
        holdings, price_fn=lambda c: 6.50, total_capital=500000
    )
    # (6.50-6.77)*10000 = -2700；-2700/500000*100 = -0.54%
    assert result == pytest.approx(-0.54, abs=0.01)


def _cfg(thr=-2.0):
    return {"loss_day_threshold_pct": thr, "loss_streak_warn_threshold": 2}


def test_update_pnl_history_append_new():
    """空历史 + 今日 -3.1% → 追加 1 条 is_loss=True。"""
    result = loss_streak.update_pnl_history(
        history=[], today=date(2026, 5, 14), pnl_pct=-3.1, cfg=_cfg()
    )
    assert len(result) == 1
    assert result[0]["date"] == "2026-05-14"
    assert result[0]["pnl_pct"] == -3.1
    assert result[0]["is_loss"] is True


def test_update_pnl_history_overwrite_same_day():
    """同日重跑覆盖。"""
    history = [{"date": "2026-05-14", "pnl_pct": -1.0, "is_loss": False}]
    result = loss_streak.update_pnl_history(
        history, today=date(2026, 5, 14), pnl_pct=-3.5, cfg=_cfg()
    )
    assert len(result) == 1
    assert result[0]["pnl_pct"] == -3.5
    assert result[0]["is_loss"] is True


def test_update_pnl_history_ring_truncate():
    """超过 10 条截断到最新 10 条。"""
    history = [
        {"date": f"2026-05-{i:02d}", "pnl_pct": 0.5, "is_loss": False}
        for i in range(1, 13)  # 12 条
    ]
    result = loss_streak.update_pnl_history(
        history, today=date(2026, 5, 14), pnl_pct=1.0, cfg=_cfg()
    )
    # 12 老 + 1 新 = 13，截断保留最近 10
    assert len(result) == 10
    assert result[-1]["date"] == "2026-05-14"


def test_update_pnl_history_threshold_from_config():
    """loss_day_threshold_pct=-3.0 时 -2.5% 不算 loss。"""
    result = loss_streak.update_pnl_history(
        [], today=date(2026, 5, 14), pnl_pct=-2.5, cfg=_cfg(thr=-3.0)
    )
    assert result[0]["is_loss"] is False


def test_count_loss_streak_empty():
    """空历史 → 0。"""
    assert loss_streak.count_loss_streak([], date(2026, 5, 14)) == 0


def test_count_loss_streak_single_loss_day():
    """单日亏 → 1。"""
    history = [{"date": "2026-05-14", "pnl_pct": -2.5, "is_loss": True}]
    assert loss_streak.count_loss_streak(history, date(2026, 5, 14)) == 1


def test_count_loss_streak_two_consecutive():
    """连续 2 日亏 → 2。"""
    history = [
        {"date": "2026-05-13", "pnl_pct": -2.3, "is_loss": True},
        {"date": "2026-05-14", "pnl_pct": -3.1, "is_loss": True},
    ]
    assert loss_streak.count_loss_streak(history, date(2026, 5, 14)) == 2


def test_count_loss_streak_reset_by_profit_day():
    """中间盈利日打断 → 不连续。"""
    history = [
        {"date": "2026-05-12", "pnl_pct": -2.3, "is_loss": True},
        {"date": "2026-05-13", "pnl_pct": +1.5, "is_loss": False},
        {"date": "2026-05-14", "pnl_pct": -3.1, "is_loss": True},
    ]
    assert loss_streak.count_loss_streak(history, date(2026, 5, 14)) == 1

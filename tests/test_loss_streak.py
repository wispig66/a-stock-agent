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

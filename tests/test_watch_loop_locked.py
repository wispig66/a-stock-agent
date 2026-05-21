"""watch_loop.evaluate() 锁仓期文案分轨测试。

watch_loop.py 位于 .agents/skills/stock-intraday/scripts/，不在 pytest pythonpath 中，
本测试通过 sys.path 手动注入后 import。
"""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "stock-intraday" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import watch_loop  # noqa: E402


def _row(code="601991", name="大唐发电", price=6.40, pct=-4.5, vol=1.0):
    """构造 fetch_spot 行 dict（字段名与 akshare 输出对齐）。"""
    return {
        "代码": code,
        "名称": name,
        "最新价": price,
        "涨跌幅": pct,
        "量比": vol,
    }


def _hold(code="601991", name="大唐发电", cost=6.77, stop_loss=6.50, unlock_date="2026-05-14"):
    """构造 hold_map 条目（与 fetch_realtime.load_holdings 输出格式一致）。"""
    return {
        "code": code, "name": name, "cost": cost, "shares": 1000,
        "buy_date": "2026-05-13", "genre": "A",
        "stop_loss": stop_loss, "take_profit": None,
        "unlock_date": unlock_date, "source": "manual", "note": "",
    }


def test_locked_hold_stop():
    """today < unlock_date 且跌破止损 → 文案改写为锁仓预案。"""
    row = _row(price=6.40)  # ≤ stop_loss=6.50
    hold_map = {"601991": _hold(unlock_date="2026-05-15")}
    today = date(2026, 5, 14)

    alerts = watch_loop.evaluate(row, watch_map={}, hold_map=hold_map, today=today)

    kinds = [k for k, _ in alerts]
    assert "hold_stop_locked" in kinds
    assert "hold_stop" not in kinds  # 不应触发解锁版
    msg = next(m for k, m in alerts if k == "hold_stop_locked")
    assert "🌙 锁仓中" in msg
    assert "明早" in msg
    assert "立即出" not in msg


def test_unlocked_hold_stop_unchanged():
    """today >= unlock_date 时 hold_stop 文案保持现状（含"立即出"）。"""
    row = _row(price=6.40)
    hold_map = {"601991": _hold(unlock_date="2026-05-13")}
    today = date(2026, 5, 14)

    alerts = watch_loop.evaluate(row, watch_map={}, hold_map=hold_map, today=today)

    kinds = [k for k, _ in alerts]
    assert "hold_stop" in kinds
    assert "hold_stop_locked" not in kinds
    msg = next(m for k, m in alerts if k == "hold_stop")
    assert "💥 持仓跌破止损" in msg
    assert "立即出" in msg
    assert "🌙" not in msg


def test_locked_hold_dump():
    """锁仓 + 砸盘 -6% → kind=hold_dump_locked，文案含"T+1 不可出"。"""
    # 价格保持在止损之上（6.60 > 6.50），避免触发 hold_stop_locked 干扰断言
    row = _row(price=6.60, pct=-6.0)
    hold_map = {"601991": _hold(unlock_date="2026-05-15")}
    today = date(2026, 5, 14)

    alerts = watch_loop.evaluate(row, watch_map={}, hold_map=hold_map, today=today)

    kinds = [k for k, _ in alerts]
    assert "hold_dump_locked" in kinds
    assert "hold_dump" not in kinds
    msg = next(m for k, m in alerts if k == "hold_dump_locked")
    assert "🌙 锁仓中" in msg
    assert "T+1 不可出" in msg


def test_unlock_date_missing_treated_as_unlocked():
    """unlock_date=None（老条目）→ 当解锁处理，走原文案。"""
    row = _row(price=6.40)
    hold_map = {"601991": _hold(unlock_date=None)}
    today = date(2026, 5, 14)

    alerts = watch_loop.evaluate(row, watch_map={}, hold_map=hold_map, today=today)

    kinds = [k for k, _ in alerts]
    assert "hold_stop" in kinds
    assert "hold_stop_locked" not in kinds

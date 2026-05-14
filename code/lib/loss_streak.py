"""连亏心态提醒：纯函数计算 + IO。

L4 盘后日跑：
1. compute_daily_pnl   算当日加权浮盈亏%
2. update_pnl_history  追加历史 + 环形截断
3. count_loss_streak   倒数连续亏损天数
4. load_state / save_state  读写 risk_state.yaml

设计：所有计算函数纯函数，IO 失败兜底不抛。
"""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = ROOT / "risk_state.yaml"

MAX_HISTORY = 10


def compute_daily_pnl(
    holdings: Iterable[dict],
    price_fn: Callable[[str], float | None],
    total_capital: float,
) -> float:
    """加权浮盈亏%（(today_value - cost_value) / total_capital × 100）。

    实时价取不到时该持仓走 cost 兜底（pnl 贡献为 0）。
    空持仓返回 0.0。
    """
    if total_capital <= 0:
        return 0.0
    pnl_value = 0.0
    for h in holdings:
        code = h.get("code")
        cost = float(h.get("cost", 0))
        shares = int(h.get("shares", 0))
        if not code or shares <= 0 or cost <= 0:
            continue
        try:
            price = price_fn(code)
        except Exception:
            price = None
        if price is None:
            continue  # cost 兜底 → 该笔 pnl=0
        pnl_value += (float(price) - cost) * shares
    return round(pnl_value / total_capital * 100, 2)

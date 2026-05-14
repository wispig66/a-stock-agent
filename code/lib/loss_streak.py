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


def update_pnl_history(
    history: list[dict],
    today: date,
    pnl_pct: float,
    cfg: dict,
) -> list[dict]:
    """追加今日记录 + 环形截断到最新 MAX_HISTORY 条。

    同日记录已存在时覆盖（重复跑 L4 不重复入库）。
    """
    threshold = float(cfg.get("loss_day_threshold_pct", -2.0))
    today_str = today.isoformat()
    new_entry = {
        "date": today_str,
        "pnl_pct": round(float(pnl_pct), 2),
        "is_loss": float(pnl_pct) < threshold,
    }
    # 去掉同日的旧条目
    filtered = [h for h in history if h.get("date") != today_str]
    filtered.append(new_entry)
    # 环形截断保留最新 MAX_HISTORY 条
    if len(filtered) > MAX_HISTORY:
        filtered = filtered[-MAX_HISTORY:]
    return filtered


def count_loss_streak(history: list[dict], today: date) -> int:
    """从 today 倒数连续 is_loss=True 的天数。

    跳过 today 之后的记录（防御）；从 ≤ today 的最近一条向前数。
    """
    today_str = today.isoformat()
    relevant = [h for h in history if h.get("date", "") <= today_str]
    if not relevant:
        return 0
    # 按日期降序
    relevant.sort(key=lambda h: h["date"], reverse=True)
    streak = 0
    for h in relevant:
        if h.get("is_loss"):
            streak += 1
        else:
            break
    return streak


def load_state(path: Path | None = None) -> dict:
    """读 risk_state.yaml；缺失或解析失败返回 {daily_pnl: []}。"""
    f = path or STATE_FILE
    if not f.exists():
        return {"daily_pnl": []}
    try:
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"[loss_streak] risk_state.yaml 解析失败 ({e})，重置为空", file=sys.stderr)
        return {"daily_pnl": []}
    raw.setdefault("daily_pnl", [])
    return raw


def save_state(state: dict, path: Path | None = None) -> None:
    """写 risk_state.yaml；失败仅 stderr，不抛。"""
    f = path or STATE_FILE
    try:
        f.write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except OSError as e:
        print(f"[loss_streak] risk_state.yaml 写入失败 ({e})", file=sys.stderr)

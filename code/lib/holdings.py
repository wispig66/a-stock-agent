"""持仓状态机。读写 holdings.yaml，提供 Holding dataclass 与 is_locked 状态。

并发控制：filelock + 原子 rename。watch_loop 读、bot_inbound 写，互不阻塞主流程。
"""
from __future__ import annotations
import os
import tempfile
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from filelock import FileLock

from lib import calendar as cal

ROOT = Path(__file__).resolve().parents[2]
HOLDINGS_FILE = ROOT / "holdings.yaml"
LOCK_FILE = ROOT / "holdings.yaml.lock"


@dataclass
class Holding:
    code: str
    name: str
    genre: str  # A / B / C / D
    cost: float
    shares: int
    buy_date: date
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    unlock_date: Optional[date] = None
    source: str = "manual"
    note: str = ""

    def __post_init__(self) -> None:
        if self.unlock_date is None:
            # 兜底：尝试用 calendar 算；算不出（历史日期超出范围）则取 buy_date 视为已解锁
            try:
                self.unlock_date = cal.next_trade_day(self.buy_date)
            except cal.CalendarOutOfRange:
                self.unlock_date = self.buy_date

    def is_locked(self, today: date) -> bool:
        return today < self.unlock_date

    def to_yaml_dict(self) -> dict:
        d = asdict(self)
        d["buy_date"] = self.buy_date.isoformat()
        d["unlock_date"] = self.unlock_date.isoformat() if self.unlock_date else None
        # 移除空字段以保持 yaml 简洁
        return {k: v for k, v in d.items() if v not in (None, "", 0) or k in ("cost", "shares")}


def _from_yaml_dict(d: dict) -> Holding:
    buy_date = _parse_date(d["buy_date"])
    unlock_date = _parse_date(d["unlock_date"]) if d.get("unlock_date") else None
    return Holding(
        code=str(d["code"]),
        name=d["name"],
        genre=d.get("genre", "未标记"),
        cost=float(d["cost"]),
        shares=int(d.get("shares", 0)),
        buy_date=buy_date,
        stop_loss=float(d["stop_loss"]) if d.get("stop_loss") is not None else None,
        take_profit=float(d["take_profit"]) if d.get("take_profit") is not None else None,
        unlock_date=unlock_date,
        source=d.get("source", "manual"),
        note=d.get("note", ""),
    )


def _parse_date(v) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def read_holdings() -> list[Holding]:
    if not HOLDINGS_FILE.exists():
        return []
    with FileLock(str(LOCK_FILE)):
        raw = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) or {}
    items = raw.get("holdings") or []
    return [_from_yaml_dict(d) for d in items if d.get("code") and d.get("name")]


def upsert_holding(new: Holding) -> Holding:
    """新增或加仓。同 code 已存在则加权均价合并，unlock_date 取最新一笔。返回最终持仓记录。"""
    with FileLock(str(LOCK_FILE)):
        raw = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) if HOLDINGS_FILE.exists() else {}
        raw = raw or {}
        items = raw.get("holdings") or []
        existing_idx = next((i for i, d in enumerate(items) if str(d.get("code")) == new.code), None)
        if existing_idx is None:
            items.append(new.to_yaml_dict())
            final = new
        else:
            old = _from_yaml_dict(items[existing_idx])
            total_shares = old.shares + new.shares
            if total_shares == 0:
                raise ValueError("合并后 shares=0，不应触发 upsert")
            merged_cost = (old.cost * old.shares + new.cost * new.shares) / total_shares
            # unlock_date 取最大（最保守）
            merged_unlock = max(old.unlock_date, new.unlock_date)
            merged = Holding(
                code=new.code,
                name=new.name,
                genre=new.genre,
                cost=round(merged_cost, 4),
                shares=total_shares,
                buy_date=new.buy_date,  # 最新一笔的 buy_date
                stop_loss=new.stop_loss if new.stop_loss is not None else old.stop_loss,
                take_profit=new.take_profit if new.take_profit is not None else old.take_profit,
                unlock_date=merged_unlock,
                source=new.source,
                note=new.note or old.note,
            )
            items[existing_idx] = merged.to_yaml_dict()
            final = merged
        raw["holdings"] = items
        _atomic_write(raw)
    return final


def remove_holding(code: str) -> Holding:
    with FileLock(str(LOCK_FILE)):
        raw = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) if HOLDINGS_FILE.exists() else {}
        raw = raw or {}
        items = raw.get("holdings") or []
        idx = next((i for i, d in enumerate(items) if str(d.get("code")) == code), None)
        if idx is None:
            raise KeyError(f"持仓中无 {code}")
        removed = _from_yaml_dict(items.pop(idx))
        raw["holdings"] = items
        _atomic_write(raw)
    return removed


def reduce_holding(code: str, shares: int) -> tuple[Holding, Holding | None]:
    """卖出部分或全部持仓。

    返回 (卖出前记录, 卖出后记录)。卖出后为 None 表示已清仓。
    """
    if shares <= 0:
        raise ValueError("shares must be > 0")
    with FileLock(str(LOCK_FILE)):
        raw = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) if HOLDINGS_FILE.exists() else {}
        raw = raw or {}
        items = raw.get("holdings") or []
        idx = next((i for i, d in enumerate(items) if str(d.get("code")) == code), None)
        if idx is None:
            raise KeyError(f"持仓中无 {code}")
        old = _from_yaml_dict(items[idx])
        if shares >= old.shares:
            items.pop(idx)
            remaining = None
        else:
            remaining = Holding(
                code=old.code,
                name=old.name,
                genre=old.genre,
                cost=old.cost,
                shares=old.shares - shares,
                buy_date=old.buy_date,
                stop_loss=old.stop_loss,
                take_profit=old.take_profit,
                unlock_date=old.unlock_date,
                source=old.source,
                note=old.note,
            )
            items[idx] = remaining.to_yaml_dict()
        raw["holdings"] = items
        _atomic_write(raw)
    return old, remaining


def _atomic_write(raw: dict) -> None:
    HOLDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".holdings.", suffix=".yaml", dir=str(HOLDINGS_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, HOLDINGS_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

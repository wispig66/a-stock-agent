"""板块四面板信息聚合 + 阶段判定 + Top 5 选股。

主入口：build_sector_pack(sector_name) -> dict
返回结构（卡片合成层消费）：
{
  "sector": "光伏",
  "stage": "主升期",
  "panels": {
      "sentiment":  {"ok": bool, "data": {...} | None, "error": str | None},
      "news":       {"ok": ..., "data": ..., "error": ...},
      "fundamental":{...},
      "technical":  {...},
  },
  "top_n":  [{code, name, role, buy_price, stop_loss, take_profit}, ...],
  "verdict_modifiers": [...],   # 单面板失败时塞 "数据缺失：xxx"
}

数据源：复用 lib/query 的 fetch_* + 本地 DB（ths_hot_reason, limit_up, fund_flow_daily）。
"""
from __future__ import annotations
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "daily.db"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import connect

# 内置同义词：用户口语 → 题材库标准名
_BUILTIN_SYNONYMS = {
    "AI": "人工智能",
    "AI算力": "算力",
    "新能源车": "新能源汽车",
}


class SectorNotFound(Exception):
    def __init__(self, query: str, candidates: list[str]):
        super().__init__(f"未找到「{query}」，建议：{candidates}")
        self.query = query
        self.candidates = candidates


# ────────── 板块名匹配 ──────────

def fuzzy_match(sector_name: str, *, synonyms: dict | None = None,
                allow_external: bool = True) -> str:
    """1. DB 精确匹配 → 2. DB 子串 → 3. 同义词替换（目标在词库中时）→ 4. raise SectorNotFound（带 3 候选）。

    allow_external=False 时跳过同义词替换，用于测试场景。
    """
    syn = synonyms if synonyms is not None else _BUILTIN_SYNONYMS
    lex = _load_lexicon()
    # 1. 精确
    if sector_name in lex:
        return sector_name
    # 2. 子串（用户输入是已知板块的子串，或已知板块是用户输入的子串）
    for name in sorted(lex, key=len, reverse=True):
        if sector_name in name or name in sector_name:
            return name
    # 3. 同义词替换（仅当目标在词库中时，且 allow_external=True）
    if allow_external:
        target = syn.get(sector_name)
        if target and target in lex:
            return target
        # 4. 候选 + 异常（仅 allow_external 模式才抛错）
        cands = _nearest(sector_name, lex, k=3)
        raise SectorNotFound(sector_name, cands)
    # allow_external=False：DB 没命中则原样返回（不抛异常）
    return sector_name


def _load_lexicon() -> set[str]:
    if not DB.exists():
        return set()
    lex: set[str] = set()
    since = (date.today() - timedelta(days=30)).isoformat()
    with connect(DB) as conn:
        for (r,) in conn.execute(
            "SELECT DISTINCT reason FROM ths_hot_reason WHERE date >= ?", (since,)
        ):
            if r:
                for piece in re.split(r"[/+、,，\s]", r):
                    p = piece.strip()
                    if p and len(p) >= 2:
                        lex.add(p)
        for (r,) in conn.execute("SELECT DISTINCT concept FROM limit_up WHERE concept IS NOT NULL"):
            if r:
                for piece in re.split(r"[/+、,，\s]", r):
                    p = piece.strip()
                    if p and len(p) >= 2:
                        lex.add(p)
    return lex


def _nearest(needle: str, lex: set[str], *, k: int = 3) -> list[str]:
    """字符重叠打分挑 Top k。零依赖。"""
    scored = sorted(
        ((len(set(needle) & set(name)), name) for name in lex),
        key=lambda x: x[0], reverse=True,
    )
    return [name for s, name in scored[:k] if s > 0]


# ────────── 四面板（每个都是独立 try/except） ──────────

def _fetch_sentiment_panel(sector: str) -> dict:
    """板块涨跌幅、5/10 日累计、涨停股数、龙头连板。
    实现：query.fetch_concept_strength + limit_up 表统计。
    （生产实现见 Task 3b；单测会 monkeypatch 桩。）"""
    raise NotImplementedError("生产环境实现 (Task 3b)；单测打桩")

def _fetch_news_panel(sector: str) -> dict:
    """ths_hot_reason 近 10 日 + news 近 7 日命中该题材的 Top 5。"""
    raise NotImplementedError("生产环境实现 (Task 3b)；单测打桩")

def _fetch_fundamental_panel(sector: str) -> dict:
    """成分股数 / 平均 PE / 所属一级行业 / 长期主线判定。"""
    raise NotImplementedError("生产环境实现 (Task 3b)；单测打桩")

def _fetch_technical_panel(sector: str) -> dict:
    """龙头票 MA / 板块指数 60 日位置 / 量比。"""
    raise NotImplementedError("生产环境实现 (Task 3b)；单测打桩")


def _safe_call(label: str, fn, *args) -> dict:
    try:
        return {"ok": True, "data": fn(*args), "error": None, "label": label}
    except Exception as e:
        return {"ok": False, "data": None, "error": f"{type(e).__name__}: {e}", "label": label}


# ────────── 阶段判定 ──────────

def classify_stage(*, ret_5d_pct: float, limit_up_count: int,
                   leader_consecutive: int, ret_3d_pct: float) -> str:
    if ret_3d_pct < 0:
        return "退潮期"
    if leader_consecutive >= 4:
        return "高潮期"
    if ret_5d_pct > 15 and limit_up_count >= 3:
        return "主升期"
    if 5 <= ret_5d_pct <= 15:
        return "启动期"
    return "退潮期"


# ────────── Top 5 选股 ──────────

def pick_top_n(candidates: list[dict], *, n: int = 5) -> list[dict]:
    """打分 = ret_5d × 0.4 + main_inflow_3d × 0.4 + dist_high_20d_pct × 0.2。
    剔除：is_st、limit_up_lock、dist_high_20d_pct < 3。"""
    def keep(c: dict) -> bool:
        if c.get("is_st"):
            return False
        if c.get("limit_up_lock"):
            return False
        if c.get("dist_high_20d_pct", 0) < 3:
            return False
        return True

    filtered = [c for c in candidates if keep(c)]
    scored = sorted(
        filtered,
        key=lambda c: c.get("ret_5d", 0) * 0.4
                      + c.get("main_inflow_3d", 0) * 0.4
                      + c.get("dist_high_20d_pct", 0) * 0.2,
        reverse=True,
    )
    return scored[:n]


# ────────── 主入口 ──────────

def build_sector_pack(sector_name: str) -> dict:
    sector = fuzzy_match(sector_name)
    panels = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(_safe_call, "sentiment",   _fetch_sentiment_panel,   sector): "sentiment",
            ex.submit(_safe_call, "news",        _fetch_news_panel,        sector): "news",
            ex.submit(_safe_call, "fundamental", _fetch_fundamental_panel, sector): "fundamental",
            ex.submit(_safe_call, "technical",   _fetch_technical_panel,   sector): "technical",
        }
        for fut in as_completed(futs):
            r = fut.result()
            panels[r["label"]] = {"ok": r["ok"], "data": r["data"], "error": r["error"]}

    modifiers = [f"数据缺失：{k}" for k, v in panels.items() if not v["ok"]]

    # stage / top_n 在面板成功时算；任何关键面板失败则降档
    s = panels.get("sentiment", {}).get("data") or {}
    stage = classify_stage(
        ret_5d_pct=s.get("ret_5d_pct", 0),
        limit_up_count=s.get("limit_up_count", 0),
        leader_consecutive=s.get("leader_consecutive", 0),
        ret_3d_pct=s.get("ret_3d_pct", 0),
    ) if panels.get("sentiment", {}).get("ok") else "未知"

    top_n = pick_top_n(s.get("candidates", []), n=5) if panels.get("sentiment", {}).get("ok") else []

    return {
        "sector": sector,
        "stage": stage,
        "panels": panels,
        "top_n": top_n,
        "verdict_modifiers": modifiers,
    }

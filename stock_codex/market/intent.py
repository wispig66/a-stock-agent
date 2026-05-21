"""意图分类：规则前置 + LLM 兜底 + 模糊安全网。

公共入口：classify(text, lexicon, llm_call=None) -> dict
- intent ∈ {stock, sector, event, ambiguous, error}
- source ∈ {explicit, rule, llm, ambiguous}

lexicon 是已知板块名集合，由调用方从 ths_hot_reason/limit_up.concept 构建并传入。
分离词库提取与分类逻辑，便于单测桩注入。
"""
from __future__ import annotations
import re
from typing import Callable, Optional

_CODE_RE = re.compile(r"^\d{6}\b")
_EXPLICIT_RE = re.compile(r"^(sector|stock|event)\s*=\s*(.*)$", re.IGNORECASE)
_EVENT_KW = ("政策", "发布会", "批了", "签了", "中标", "刚发", "宣布", "发布", "出台")
_LLM_CONF_THRESHOLD = 0.6


def classify(
    text: str,
    *,
    lexicon: set[str],
    llm_call: Optional[Callable[[str], dict]] = None,
) -> dict:
    """返回 {intent, extracted, confidence, source}。"""
    t = text.strip()

    # ────────── 1. 显式覆盖 ──────────
    m = _EXPLICIT_RE.match(t)
    if m:
        kind = m.group(1).lower()
        val = m.group(2).strip()
        if not val:
            return {"intent": "error", "extracted": f"{kind}= 后面缺值",
                    "confidence": 1.0, "source": "explicit"}
        return {"intent": kind, "extracted": val,
                "confidence": 1.0, "source": "explicit"}

    # ────────── 2. 规则匹配 ──────────
    # 2a. 6 位数字开头 → 个股
    m = _CODE_RE.match(t)
    if m:
        return {"intent": "stock", "extracted": m.group(0),
                "confidence": 1.0, "source": "rule"}

    # 2b. 事件关键词（优先于板块匹配：含事件词的文本即使提到板块名也是事件）
    if any(kw in t for kw in _EVENT_KW):
        return {"intent": "event", "extracted": t,
                "confidence": 0.85, "source": "rule"}

    # 2c. 命中已知板块名（精确或子串，长名优先匹配避免 "AI" 抢 "AI算力" 错配）
    hit = _match_sector(t, lexicon)
    if hit:
        return {"intent": "sector", "extracted": hit,
                "confidence": 0.95, "source": "rule"}

    # ────────── 3. LLM 兜底 ──────────
    if llm_call is not None:
        try:
            llm_resp = llm_call(t)
            conf = float(llm_resp.get("confidence", 0.0))
            it = llm_resp.get("intent", "")
            extracted = llm_resp.get("extracted", t)
            if conf >= _LLM_CONF_THRESHOLD and it in ("stock", "sector", "event"):
                return {"intent": it, "extracted": extracted,
                        "confidence": conf, "source": "llm"}
        except Exception:
            pass  # 落到模糊兜底

    # ────────── 4. 模糊兜底 ──────────
    return _ambiguous(t, lexicon)


def _match_sector(t: str, lexicon: set[str]) -> Optional[str]:
    """长名优先：从最长到最短遍历，子串命中即返回。"""
    if not lexicon:
        return None
    for name in sorted(lexicon, key=len, reverse=True):
        if name in t:
            return name
    return None


def _ambiguous(t: str, lexicon: set[str]) -> dict:
    """给用户 3 个候选让其重发命令。"""
    candidates = []
    # 候选 1：从词库挑最相似板块
    near = _nearest_sector(t, lexicon)
    if near:
        candidates.append({"label": "A", "kind": "sector", "value": near})
    # 候选 2：当个股名查（让 stock-query 自己 lookup_by_name）
    candidates.append({"label": "B", "kind": "stock", "value": t[:40]})
    # 候选 3：当事件
    candidates.append({"label": "C", "kind": "event", "value": t[:80]})
    return {
        "intent": "ambiguous",
        "extracted": {"original": t, "candidates": candidates},
        "confidence": 0.0,
        "source": "ambiguous",
    }


def _nearest_sector(t: str, lexicon: set[str]) -> Optional[str]:
    """最简朴的关键词重叠打分；零依赖。"""
    if not lexicon:
        return None
    chars = set(t)
    best, best_score = None, 0
    for name in lexicon:
        score = len(chars & set(name))
        if score > best_score:
            best, best_score = name, score
    return best


def build_llm_prompt(text: str) -> str:
    """供 SKILL.md 引用的标准 prompt，避免散在多处。"""
    return (
        "判断用户问的是 A 股的：\n"
        "1. 个股（给出 code 或 name）\n"
        "2. 板块/题材（给出 sector 名）\n"
        "3. 事件（给出事件文本）\n"
        "4. 模糊（无法明确）\n\n"
        f"用户输入：{text}\n"
        '严格输出 JSON: {"intent": "stock|sector|event|ambiguous", '
        '"extracted": "...", "confidence": 0.0-1.0}'
    )


def build_sector_lexicon(db_path) -> set[str]:
    """从 ths_hot_reason.reason 近 30 日 + limit_up.concept 历史 构建板块词库。
    生产代码调用；单测桩用固定集合。"""
    import sqlite3
    from datetime import date, timedelta
    lex: set[str] = set()
    with sqlite3.connect(db_path) as conn:
        since = (date.today() - timedelta(days=30)).isoformat()
        rows = conn.execute(
            "SELECT DISTINCT reason FROM ths_hot_reason WHERE date >= ?", (since,)
        ).fetchall()
        for (r,) in rows:
            if r:
                # reason 字段可能含多个题材用 / 或 + 分隔
                for piece in re.split(r"[/+、,，\s]", r):
                    p = piece.strip()
                    if p and len(p) >= 2:
                        lex.add(p)
        rows = conn.execute("SELECT DISTINCT concept FROM limit_up WHERE concept IS NOT NULL").fetchall()
        for (r,) in rows:
            if r:
                for piece in re.split(r"[/+、,，\s]", r):
                    p = piece.strip()
                    if p and len(p) >= 2:
                        lex.add(p)
    return lex

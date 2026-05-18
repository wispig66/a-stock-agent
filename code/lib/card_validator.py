"""卡片数据来源校验。

强制规则：stock skill 卡片里出现的每个数据点都必须能在 fact pack 的 ALLOWED 段
找到对应字段。见 [[feedback-data-must-be-sourced]] 和 docs/allowed_schema.md。

调用方：scripts/tg_listener.py 在 _tg_edit 推卡前调一次 validate_card()。
mode=warn 仅打日志不拦截；mode=enforce 失败拒推。
"""
from __future__ import annotations
import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional

# 容差（由 feedback_data_must_be_sourced 5/18 事故定）
PCT_TOLERANCE = 0.5          # 涨跌幅 ±0.5%
NEWS_RATIO_THRESHOLD = 0.55  # 新闻标题 SequenceMatcher 阈值（卡片常做摘要/截断，放宽到 0.55）

# 不视作"股名"的常见中文 3+ 字词（避免误伤）
_COMMON_NON_STOCK_WORDS = frozenset({
    "创业板", "上证综指", "深证成指", "科创板", "中小板", "主板",
    "证监会", "工信部", "发改委", "国务院", "财政部", "央行",
    "申万一级", "申万二级", "中信一级",
    "市场情绪", "市场怎么走", "题材延续", "高度梯队", "外围利空",
    "今日空仓", "明早竞价", "明日预案",
})

_CODE_RE = re.compile(r"(?<![\d.])\b(\d{6})\b(?!\.\d)")  # 6 位数字，前后非小数点上下文
_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
_LIANBAN_RE = re.compile(r"(\d+)\s*(?:连)?板")
_LIMIT_UP_COUNT_RE = re.compile(r"涨停\s*(\d+)\s*只")
_BROKEN_COUNT_RE = re.compile(r"炸板\s*(\d+)\s*只")


@dataclass
class Violation:
    kind: str           # unknown_code / unknown_name / lianban_mismatch / pct_mismatch / summary_mismatch / unknown_news
    target: str         # 触发的具体 token（"601991"/"大唐发电"/"涨停 75 只"…）
    expected: str = ""  # ALLOWED 里对应的真值（如有）
    detail: str = ""    # 上下文片段，便于人工 review

    def to_dict(self) -> dict:
        return {"kind": self.kind, "target": self.target,
                "expected": self.expected, "detail": self.detail[:200]}


def load_stock_name_dict(db_path: str | Path) -> dict[str, str]:
    """读 stock_basic 全表，返回 {name: code}。供反查模型编出来的"中文股名"。"""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT code, name FROM stock_basic").fetchall()
    return {n: c for c, n in rows if n}


def _strip_tags(s: str) -> str:
    """剥 HTML <b>/<i>/<a>… 标签，保留文本。"""
    return re.sub(r"<[^>]+>", "", s)


def _nearest_code(text: str, pos: int, all_codes_positions: list[tuple[int, str]],
                  max_dist: int = 60) -> Optional[str]:
    """找距离 pos 最近的 code（必须 < max_dist 字且二者之间没有其他 code）。"""
    best = None
    best_dist = max_dist + 1
    for cpos, code in all_codes_positions:
        d = abs(cpos - pos)
        if d < best_dist:
            best = code
            best_dist = d
    return best


def _associate_tokens_with_codes(card_no_tags: str, pattern: re.Pattern,
                                 known_codes: set[str]) -> list[tuple[str, str, int]]:
    """通用："对每个 pattern 命中，找离它最近的 code"。返回 [(code, matched_value, abs_pos)]。

    只在 pattern 命中位置和最近 code 之间不夹其他 code 时才绑定。
    """
    code_positions = [(m.start(), m.group(1)) for m in _CODE_RE.finditer(card_no_tags)
                      if m.group(1) in known_codes]
    out = []
    for m in pattern.finditer(card_no_tags):
        pos = m.start()
        # 找前向最近的 code，距离 < 60 字
        best = None
        best_dist = 61
        for cpos, code in code_positions:
            if cpos > pos:
                continue
            d = pos - cpos
            if d < best_dist:
                best = (code, cpos)
                best_dist = d
        if best is None:
            continue
        code, cpos = best
        # 验证 code 和 pos 之间不夹其他 code
        between = [c for cp, c in code_positions if cpos < cp < pos]
        if between:
            continue
        out.append((code, m.group(1), pos))
    return out


def validate_card(
    card_text: str,
    allowed: dict,
    stock_name_dict: Optional[dict[str, str]] = None,
) -> tuple[bool, list[Violation]]:
    """检查 card_text 里所有数据点是否都在 allowed 里。

    allowed schema（见 docs/allowed_schema.md）:
        codes:    {"001259": "利仁科技", ...}      必填
        lianban:  {"001259": 6, ...}              可选；缺则不校验连板
        pct:      {"603082": 10.0, ...}           可选；缺则不校验涨跌幅
        summary:  {"limit_up": 78, "broken": 36, ...}  可选
        concepts: ["AI算力", ...]                 可选
        news:     [{"title": "...", ...}]         可选；非空则启用 title 校验
    返回 (ok, violations)。violations 为空 ⇔ ok=True。
    """
    text = _strip_tags(card_text)
    violations: list[Violation] = []

    allowed_codes: dict[str, str] = allowed.get("codes") or {}
    allowed_names: set[str] = set(allowed_codes.values())

    # 规则 1：6 位数字必须在 allowed.codes
    for m in _CODE_RE.finditer(text):
        code = m.group(1)
        if code in allowed_codes:
            continue
        ctx_start = max(0, m.start() - 30)
        ctx_end = min(len(text), m.end() + 30)
        # 排除明显是价位 / 时间 / 容量等非股票上下文
        ctx = text[ctx_start:ctx_end]
        if re.search(r"(\d{2,3}[年月日时分秒])|价格|股价", ctx):
            continue
        violations.append(Violation(
            kind="unknown_code", target=code, detail=ctx,
        ))

    # 规则 2：中文股名必须在 allowed.names（基于 stock_basic 词典反查）
    if stock_name_dict is not None:
        for name, code in stock_name_dict.items():
            if len(name) < 3:
                continue  # 太短易误伤
            if name in _COMMON_NON_STOCK_WORDS:
                continue
            if name not in text:
                continue
            if name in allowed_names:
                continue
            idx = text.index(name)
            ctx = text[max(0, idx - 30):idx + len(name) + 30]
            violations.append(Violation(
                kind="unknown_name", target=name,
                expected=f"未在 ALLOWED.codes 中（对应代码 {code}）", detail=ctx,
            ))

    # 规则 3：连板数对齐（"N 板"绑定到前向最近的 code）
    lianban_map: dict[str, int] = {str(k): int(v) for k, v in (allowed.get("lianban") or {}).items()}
    for code, raw_val, pos in _associate_tokens_with_codes(
            text, _LIANBAN_RE, set(allowed_codes.keys())):
        try:
            got = int(raw_val)
        except ValueError:
            continue
        if code not in lianban_map:
            continue  # 该 code 不在连板表里（非涨停股），跳过
        if got != lianban_map[code]:
            ctx = text[max(0, pos - 40):pos + 40]
            violations.append(Violation(
                kind="lianban_mismatch", target=f"{code} {got}板",
                expected=f"{lianban_map[code]}板", detail=ctx,
            ))

    # 规则 4：涨跌幅对齐（±PCT_TOLERANCE，绑定前向最近 code）
    pct_map: dict[str, float] = {str(k): float(v) for k, v in (allowed.get("pct") or {}).items()}
    for code, raw_val, pos in _associate_tokens_with_codes(
            text, _PCT_RE, set(allowed_codes.keys())):
        try:
            got = float(raw_val)
        except ValueError:
            continue
        if code not in pct_map:
            continue
        # 排除买点/止损/封单等不是当日涨跌幅的 % 上下文
        ctx_pre = text[max(0, pos - 30):pos]
        if re.search(r"(买点|止损|止盈|封单|换手|量比|占|权重|目标)", ctx_pre):
            continue
        if abs(got - pct_map[code]) > PCT_TOLERANCE:
            ctx = text[max(0, pos - 40):pos + 40]
            violations.append(Violation(
                kind="pct_mismatch", target=f"{code} {got:+.2f}%",
                expected=f"{pct_map[code]:+.2f}%", detail=ctx,
            ))

    # 规则 5：summary 总数精确对齐
    summary = allowed.get("summary") or {}
    if "limit_up" in summary:
        for m in _LIMIT_UP_COUNT_RE.finditer(text):
            got = int(m.group(1))
            if got != summary["limit_up"]:
                ctx = text[max(0, m.start() - 30):m.end() + 30]
                violations.append(Violation(
                    kind="summary_mismatch", target=f"涨停 {got} 只",
                    expected=f"{summary['limit_up']} 只", detail=ctx,
                ))
    if "broken" in summary:
        for m in _BROKEN_COUNT_RE.finditer(text):
            got = int(m.group(1))
            if got != summary["broken"]:
                ctx = text[max(0, m.start() - 30):m.end() + 30]
                violations.append(Violation(
                    kind="summary_mismatch", target=f"炸板 {got} 只",
                    expected=f"{summary['broken']} 只", detail=ctx,
                ))

    # 规则 6：新闻标题相似度校验（仅对带"→"且无 6 位代码的行做校验，避免误伤观察池行）
    news = allowed.get("news") or []
    if news:
        news_titles = [n.get("title", "") for n in news if n.get("title")]
        for raw in text.splitlines():
            line = raw.strip()
            # 必须是新闻条目格式：以 - / · / • 开头，且含 →
            if not re.match(r"^[\-·•]\s", line):
                continue
            if "→" not in line:
                continue
            # 含 6 位代码 = 观察池行，不是新闻
            if _CODE_RE.search(line):
                continue
            head = line.split("→")[0].strip().lstrip("-·• ").strip()
            if len(head) < 8:
                continue
            # 排除明显不是新闻的行（含买卖纪律词）
            if re.search(r"(板\b|买点|止损|止盈|封单|龙头|涨停|跌停)", head):
                continue
            def _news_score(h: str, t: str) -> float:
                ratio = SequenceMatcher(None, h, t).ratio()
                # 加分：head 是 title 的子串（卡片做摘要的典型情形）
                if h in t or t in h:
                    return max(ratio, 0.8)
                # 加分：长公共子串占短串比例 ≥ 50%
                match = SequenceMatcher(None, h, t).find_longest_match(0, len(h), 0, len(t))
                if match.size >= min(len(h), len(t)) * 0.5:
                    return max(ratio, 0.7)
                return ratio
            best = max((_news_score(head, t) for t in news_titles), default=0.0)
            if best < NEWS_RATIO_THRESHOLD:
                violations.append(Violation(
                    kind="unknown_news", target=head[:60],
                    expected=f"无 title 相似度 ≥{NEWS_RATIO_THRESHOLD} 的新闻",
                    detail=line[:200],
                ))

    return (len(violations) == 0, violations)


def format_violations(violations: Iterable[Violation], limit: int = 10) -> str:
    """人类可读的违规清单，用于 TG 提示卡 / 审计日志。"""
    lines = []
    for v in list(violations)[:limit]:
        if v.expected:
            lines.append(f"  · [{v.kind}] {v.target} → 应为 {v.expected}")
        else:
            lines.append(f"  · [{v.kind}] {v.target}")
    return "\n".join(lines)

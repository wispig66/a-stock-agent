"""事件解读 pack：LLM 归类 → 题材库校准 → (deep) web 补强 → 各板块跑 sector_pack → 合成。

主入口：build_event_pack(event_text, *, mode, categorize, sector_pack_fn, web_fetch=None)
- categorize:      callable(text) -> {event_type, candidate_sectors[], risk_sectors[], core_logic}
- sector_pack_fn:  callable(sector_name) -> dict（即 sector_pack.build_sector_pack；可桩）
- web_fetch:       仅 deep 模式；callable(query, timeout) -> list[str]（板块名）
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Literal, Optional

from lib import sector_pack

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "daily.db"

WEB_TIMEOUT_SEC = 45


def calibrate(sectors: list[str], lexicon: set[str]) -> list[dict]:
    """对每个候选板块标三档：verified / near / miss。"""
    result = []
    for raw in sectors:
        match = "miss"
        canonical = raw
        # ✓ 完全/子串命中
        for name in sorted(lexicon, key=len, reverse=True):
            if raw in name or name in raw:
                match = "verified"
                canonical = name
                break
        # △ 近似（字符重叠 >= 2）
        if match == "miss":
            for name in lexicon:
                if len(set(raw) & set(name)) >= 2:
                    match = "near"
                    canonical = name
                    break
        result.append({"raw": raw, "name": canonical, "calibration": match})
    return result


def build_event_pack(
    event_text: str,
    *,
    mode: Literal["normal", "deep"],
    categorize: Callable[[str], dict],
    sector_pack_fn: Callable[[str], dict],
    web_fetch: Optional[Callable[[str, int], list[str]]] = None,
) -> dict:
    # Step 1: 事件归类
    cat = categorize(event_text)

    # Step 2: 题材库校准
    lex = sector_pack._load_lexicon()
    benefit_raw = calibrate(cat.get("candidate_sectors", []), lex)
    risk_raw    = calibrate(cat.get("risk_sectors", []), lex)

    benefit = [s for s in benefit_raw if s["calibration"] in ("verified", "near")]
    # risk sectors: always list all (verified/near/miss), they are informational only
    risk = risk_raw

    degraded = False
    if not benefit:
        # 全 miss → 保留前 2 个、降档
        benefit = benefit_raw[:2]
        degraded = True

    # Step 3 (deep only): web 补强
    web_status = "skipped"
    if mode == "deep" and web_fetch is not None:
        try:
            extra = web_fetch(event_text, WEB_TIMEOUT_SEC)
            web_status = "ok"
            existing = {b["name"] for b in benefit}
            for raw in extra:
                if raw not in existing:
                    cal = calibrate([raw], lex)[0]
                    cal["calibration"] = "web"
                    benefit.append(cal)
        except Exception:
            web_status = "timeout"

    # Step 4: 并发跑每个受益板块的 sector_pack
    sector_packs: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(sector_pack_fn, s["name"]): s["name"] for s in benefit}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                sector_packs[name] = fut.result()
            except Exception as e:
                sector_packs[name] = {"sector": name, "error": str(e), "stage": "未知", "top_n": []}

    # Step 5: 综合打分 → 首选板块 → 推荐标的（每板块取 Top 2-3）
    def stage_score(stage: str) -> int:
        return {"启动期": 3, "主升期": 2, "高潮期": 1, "退潮期": 0, "未知": 0}.get(stage, 0)

    ranked = sorted(
        benefit,
        key=lambda s: stage_score(sector_packs.get(s["name"], {}).get("stage", "未知")),
        reverse=True,
    )
    primary = ranked[:2]  # 首选 1-2 个

    recommendations = []
    for s in primary:
        sp = sector_packs.get(s["name"], {})
        for stock in (sp.get("top_n") or [])[:2]:
            recommendations.append({
                "sector": s["name"],
                "code": stock.get("code"),
                "name": stock.get("name"),
                "role": stock.get("role"),
                "buy_price": stock.get("buy_price"),
                "stop_loss": stock.get("stop_loss"),
            })

    return {
        "event_text": event_text,
        "event_type": cat.get("event_type", "未知"),
        "core_logic": cat.get("core_logic", ""),
        "benefit_sectors": benefit,
        "risk_sectors": risk,           # 仅列名，不推荐做空
        "sector_packs": sector_packs,
        "recommendations": recommendations,
        "degraded": degraded,
        "mode": mode,
        "web_status": web_status,
    }

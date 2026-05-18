"""A 股随时分析 fact pack 生成器。

被 stock-ask SKILL.md 调用。一次 Python 进程并发拉所有数据，输出 JSON fact pack
让 claude 一次性综合判断 + 写卡片。

用法:
  uv run scripts/stock_ask_pipeline.py --text "token工厂" --mode normal

输出：JSON 到 stdout（claude 直接读）+ 写文件 data/ask_fact_pack/<ts>.json
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "stock-premarket" / "scripts"))

from logger import get_logger, init_req_id_from_env  # noqa: E402

init_req_id_from_env()
log = get_logger("stock_ask_pipeline")

DB = ROOT / "data" / "daily.db"
TASK_TIMEOUT = 25  # 单 task 硬上限


def task_lexicon(text: str) -> dict:
    """题材库相似度 Top 3 + 规则分类预判（本地 DB，<1s）。"""
    from lib import intent, sector_pack
    lex = sector_pack._load_lexicon()
    r = intent.classify(text, lexicon=lex, llm_call=None)
    nearest = sector_pack._nearest(text, lex, k=3) if lex else []
    return {
        "rule_intent": r["intent"],
        "rule_extracted": r["extracted"] if isinstance(r["extracted"], str) else None,
        "rule_confidence": r["confidence"],
        "nearest_sectors": nearest,
        "lexicon_size": len(lex),
    }


def task_stock_match(text: str) -> dict:
    """个股精确匹配（本地 DB，<1s）。"""
    from lib import query
    kind, val = query.parse_input(text)
    if kind == "code":
        board = query.board_of(val)
        if board:
            return {
                "matched": True, "code": val, "name": None,
                "board": board, "is_st": query.is_st(val),
                "via": "code",
            }
    elif kind == "name":
        hits = query.lookup_by_name(val)
        if len(hits) == 1:
            code, name = hits[0]
            return {
                "matched": True, "code": code, "name": name,
                "board": query.board_of(code), "is_st": query.is_st(code),
                "via": "name_unique",
            }
        if len(hits) > 1:
            return {
                "matched": False, "via": "name_ambiguous",
                "candidates": [{"code": c, "name": n} for c, n in hits[:5]],
            }
    return {"matched": False, "via": "none"}


def task_db_frequency(text: str) -> dict:
    """ths_hot_reason / limit_up.concept 近 7 日 LIKE 命中频次 + 示例。"""
    if not DB.exists():
        return {"ths_hot_reason_hits": 0, "limit_up_concept_hits": 0,
                "sample_reasons": [], "error": "DB 不存在"}
    since = (date.today() - timedelta(days=7)).isoformat()
    pattern = f"%{text}%"
    with sqlite3.connect(DB) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        n_reason = 0
        n_concept = 0
        sample_reasons: list[str] = []
        sample_concepts: list[str] = []
        try:
            n_reason = conn.execute(
                "SELECT COUNT(*) FROM ths_hot_reason WHERE date >= ? AND reason LIKE ?",
                (since, pattern),
            ).fetchone()[0]
            if n_reason > 0:
                sample_reasons = [r[0] for r in conn.execute(
                    "SELECT DISTINCT reason FROM ths_hot_reason "
                    "WHERE date >= ? AND reason LIKE ? LIMIT 5",
                    (since, pattern),
                )]
        except sqlite3.OperationalError:
            pass
        try:
            n_concept = conn.execute(
                "SELECT COUNT(*) FROM limit_up WHERE date >= ? AND concept LIKE ?",
                (since, pattern),
            ).fetchone()[0]
            if n_concept > 0:
                sample_concepts = [r[0] for r in conn.execute(
                    "SELECT DISTINCT concept FROM limit_up "
                    "WHERE date >= ? AND concept LIKE ? LIMIT 5",
                    (since, pattern),
                )]
        except sqlite3.OperationalError:
            pass
    return {
        "ths_hot_reason_hits": n_reason,
        "limit_up_concept_hits": n_concept,
        "sample_reasons": sample_reasons,
        "sample_concepts": sample_concepts,
        "since_date": since,
    }


def task_local_news(text: str) -> dict:
    """akshare 已爬的隔夜消息中含 TEXT 关键词的标题（~3-10s）。"""
    try:
        from fetch_data import fetch_overnight_news
    except ImportError as e:
        return {"news": [], "error": f"import: {e}"}
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        df = fetch_overnight_news(today_str)
        if df is None or df.empty:
            return {"news": [], "total_scanned": 0}
        mask = df["标题"].astype(str).str.contains(text, na=False, case=False)
        hits = df[mask].head(8)
        return {
            "news": [
                {
                    "title": str(r.get("标题", ""))[:200],
                    "url": str(r.get("URL", "") or ""),
                    "source": str(r.get("来源", "")),
                    "publish_time": str(r.get("发布时间", "")),
                }
                for r in hits.to_dict("records")
            ],
            "total_scanned": len(df),
        }
    except Exception as e:
        log.exception("local_news 异常")
        return {"news": [], "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--mode", default="normal", choices=["normal", "deep"])
    args = ap.parse_args()

    text = args.text.strip()
    if not text:
        print(json.dumps({"error": "empty text"}, ensure_ascii=False))
        sys.exit(2)

    log.info("pipeline 启动 text=%s mode=%s", text, args.mode)
    t0 = time.time()

    tasks = {
        "lexicon":      task_lexicon,
        "stock_match":  task_stock_match,
        "db_frequency": task_db_frequency,
        "local_news":   task_local_news,
    }
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fn, text): name for name, fn in tasks.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                results[name] = fut.result(timeout=TASK_TIMEOUT)
            except Exception as e:
                log.exception("task %s 异常", name)
                results[name] = {"error": f"{type(e).__name__}: {e}"}

    elapsed = round(time.time() - t0, 2)
    fact_pack = {
        "text": text,
        "mode": args.mode,
        "elapsed_sec": elapsed,
        **results,
    }

    out_dir = ROOT / "data" / "ask_fact_pack"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{int(time.time())}.json"
    out_file.write_text(json.dumps(fact_pack, ensure_ascii=False, indent=2))

    print(json.dumps(fact_pack, ensure_ascii=False, indent=2))
    log.info("pipeline 完成 用时 %.1fs file=%s", elapsed, out_file.name)


if __name__ == "__main__":
    main()

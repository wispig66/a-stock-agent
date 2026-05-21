"""单股 fact pack 生成器。

被 stock-query SKILL.md 调用。一次 Python 进程并发拉所有数据，每个数据源独立 try/except，
输出 JSON fact pack 让 Codex 一次性综合判断 + 写卡片。

用法:
  uv run scripts/stock_query_pipeline.py --code 002208 --mode fresh
  uv run scripts/stock_query_pipeline.py --code 002208 --mode holding

输出：JSON 到 stdout + 写文件 data/query_fact_pack/<ts>_<code>.json
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from stock_codex.infra.logger import get_logger, init_req_id_from_env  # noqa: E402
from stock_codex.paths import DATA_DIR, DB_FILE

init_req_id_from_env()
log = get_logger("stock_query_pipeline")

TASK_TIMEOUT = 15  # 单 task 硬上限


def _wrap(fn, *args, **kwargs) -> dict:
    """统一包装：捕获所有异常，返回 {ok, data/error, elapsed_sec}。"""
    t = time.time()
    try:
        data = fn(*args, **kwargs)
        return {"ok": True, "data": data, "elapsed_sec": round(time.time() - t, 2)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "elapsed_sec": round(time.time() - t, 2)}


def task_realtime(code: str) -> dict:
    from stock_codex.market import query
    return _wrap(query.fetch_realtime, code)


def task_kline(code: str) -> dict:
    from stock_codex.market import query
    def _fetch():
        df = query.fetch_kline(code, days=60)
        return df.to_dict("records") if df is not None and not df.empty else []
    return _wrap(_fetch)


def task_concept(code: str) -> dict:
    from stock_codex.market import query
    return _wrap(query.fetch_concept_strength, code)


def task_money_flow(code: str) -> dict:
    from stock_codex.market import query
    def _fetch():
        df = query.fetch_money_flow(code, days=5)
        return df.to_dict("records") if df is not None and not df.empty else []
    return _wrap(_fetch)


def task_news(code: str) -> dict:
    from stock_codex.market import query
    return _wrap(query.fetch_recent_news, code, 7)


def task_meta(code: str) -> dict:
    """本地 DB：板块归属 / 是否 ST。"""
    from stock_codex.market import query
    def _fetch():
        return {"board": query.board_of(code), "is_st": query.is_st(code)}
    return _wrap(_fetch)


def task_holding(code: str) -> dict:
    """读 holdings.yaml 找当前 code 持仓。未持仓 ok=true data=None。"""
    def _fetch():
        from stock_codex.domain.holdings import read_holdings
        from datetime import date
        for h in read_holdings():
            if h.code == code:
                return {
                    "code": h.code,
                    "name": h.name,
                    "genre": h.genre,
                    "cost": h.cost,
                    "shares": h.shares,
                    "buy_date": h.buy_date.isoformat(),
                    "stop_loss": h.stop_loss,
                    "take_profit": h.take_profit,
                    "unlock_date": h.unlock_date.isoformat() if h.unlock_date else None,
                    "is_locked": h.is_locked(date.today()),
                    "note": h.note,
                }
        return None
    return _wrap(_fetch)


def task_ths_hot_reasons(code: str) -> dict:
    """近 10 日 ths_hot_reason 表里出现该 code 的题材名（小数据，<200ms）。"""
    import sqlite3
    from datetime import date, timedelta
    def _fetch():
        db = DB_FILE
        if not db.exists():
            return []
        since = (date.today() - timedelta(days=10)).isoformat()
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                rows = conn.execute(
                    "SELECT DISTINCT date, reason FROM ths_hot_reason "
                    "WHERE code = ? AND date >= ? ORDER BY date DESC LIMIT 10",
                    (code, since),
                ).fetchall()
            except sqlite3.OperationalError as e:
                raise RuntimeError(f"DB query failed: {e}") from e
        return [{"date": d, "reason": r} for d, r in rows]
    return _wrap(_fetch)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--mode", default="fresh", choices=["fresh", "holding"])
    args = ap.parse_args()

    code = args.code.strip()
    if not code.isdigit() or len(code) != 6:
        print(json.dumps({"error": f"非法 code: {code}"}, ensure_ascii=False))
        sys.exit(2)

    log.info("pipeline 启动 code=%s mode=%s", code, args.mode)
    t0 = time.time()

    tasks = {
        "realtime":         task_realtime,
        "kline":            task_kline,
        "concept":          task_concept,
        "money_flow":       task_money_flow,
        "news":             task_news,
        "meta":             task_meta,
        "ths_hot_reasons":  task_ths_hot_reasons,
    }
    if args.mode == "holding":
        tasks["holding"] = task_holding

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = {ex.submit(fn, code): name for name, fn in tasks.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                results[name] = fut.result(timeout=TASK_TIMEOUT)
            except Exception as e:
                log.exception("task %s 异常", name)
                results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}",
                                 "elapsed_sec": None}

    name = None
    if results.get("realtime", {}).get("ok"):
        name = results["realtime"]["data"].get("name")

    elapsed = round(time.time() - t0, 2)
    fact_pack = {
        "code": code,
        "name": name,
        "mode": args.mode,
        "elapsed_sec": elapsed,
        **results,
    }

    out_dir = DATA_DIR / "query_fact_pack"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{int(time.time())}_{code}.json"
    out_file.write_text(json.dumps(fact_pack, ensure_ascii=False, indent=2))

    print(json.dumps(fact_pack, ensure_ascii=False, indent=2))
    log.info("pipeline 完成 用时 %.1fs file=%s", elapsed, out_file.name)

    # ALLOWED 段：单股卡片校验事实清单
    from datetime import datetime, date
    codes: dict[str, str] = {code: name or ""}
    pct: dict[str, float] = {}
    concepts: list[str] = []

    rt = results.get("realtime") or {}
    if rt.get("ok"):
        try:
            d = rt["data"]
            pct[code] = round((float(d["close"]) / float(d["pre_close"]) - 1) * 100, 2)
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            pass

    # 概念榜里的其他股（龙头等）+ 题材名
    conc = results.get("concept") or {}
    if conc.get("ok"):
        data = conc.get("data") or {}
        for c in (data.get("top_concepts") or []):
            cn = c.get("concept_name")
            if cn and cn not in concepts:
                concepts.append(cn)

    # ths_hot_reasons 历史命中的题材名
    thsr = results.get("ths_hot_reasons") or {}
    for r in (thsr.get("data") or []):
        reason = str(r.get("reason", "") or "").strip()
        for t in (x.strip() for x in reason.split("+") if x.strip()):
            if t not in concepts:
                concepts.append(t)

    # 新闻
    news_out: list[dict] = []
    nw = results.get("news") or {}
    for n in (nw.get("data") or []):
        news_out.append({
            "title": str(n.get("title") or "")[:200],
            "url": str(n.get("url") or ""),
            "time": str(n.get("date") or ""),
        })

    holding_summary = None
    h = results.get("holding") or {}
    if h.get("ok") and h.get("data"):
        hd = h["data"]
        holding_summary = {"cost": hd.get("cost"), "shares": hd.get("shares"),
                           "stop_loss": hd.get("stop_loss"),
                           "take_profit": hd.get("take_profit"),
                           "is_locked": hd.get("is_locked")}

    allowed = {
        "schema_version": "1",
        "skill": "stock-query",
        "snapshot_at": datetime.now().replace(microsecond=0).isoformat(),
        "codes": codes,
        "lianban": {},  # 单股不评连板
        "pct": pct,
        "summary": {
            "date": date.today().isoformat(),
            "code": code, "name": name, "mode": args.mode,
            "holding": holding_summary,
        },
        "concepts": concepts[:30],
        "news": news_out,
        "global_markets": {},
    }
    allowed_file = DATA_DIR / "allowed_latest_stock-query.json"
    allowed_file.parent.mkdir(parents=True, exist_ok=True)
    allowed_file.write_text(json.dumps(allowed, ensure_ascii=False, indent=2))
    log.info("ALLOWED 已写 %s codes=%d concepts=%d", allowed_file.name, len(codes), len(concepts))


if __name__ == "__main__":
    main()

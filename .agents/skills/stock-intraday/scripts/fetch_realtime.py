"""
盘中实时数据拉取：观察池 + 持仓 + 实时行情。

用法：
    python fetch_realtime.py              # 纪律分支（09:30 / 09:45）
    python fetch_realtime.py --halfday    # 11:30 半日分支，额外拉涨停结构 + 概念热度
    python fetch_realtime.py --endday     # 14:30 尾盘快照分支（非收盘数据）
"""

from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
import akshare as ak

warnings.filterwarnings("ignore")
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)

ROOT = Path(__file__).resolve().parents[4]
DB = ROOT / "data" / "daily.db"
HOLDINGS_FILE = ROOT / "holdings.yaml"
CACHE_DIR = ROOT / "data" / "intraday_cache"
MARKET_SNAPSHOT_STALE_SECONDS = 10 * 60

from stock_codex.infra.db import connect as db_connect  # noqa: E402


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def section(title: str):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def load_today_watchlist() -> list[dict]:
    """读取今日盘前决策单 + 盘中动态趋势池，缺失时回退旧 stock-premarket 推送。

    Fallback 链：
    1. decision_tickets 今日决策单（盘前交易决策漏斗）
    2. watchlist_dynamic 手工 /watch 池（仅接受买点和止损完整记录）
    3. push_log 今日 stock-premarket（旧观察池）
    4. data/last_card.md（mtime=今天，已写卡未推送，比如 push 链路慢）
    5. 都没有 → 返回空，并打 PREMARKET_MISSING 标记给上游 SKILL 走兜底文案
    """
    today = datetime.now().strftime("%Y-%m-%d")
    items: list[dict] = []
    try:
        from stock_codex.domain.decision import load_watchlist_compat
        decision_items = load_watchlist_compat(DB, today)
        if decision_items:
            log("[info] watchlist 数据来源：decision_tickets")
            items.extend(decision_items)
    except Exception as e:
        log(f"[warn] decision_tickets 读取失败，回退旧观察池：{e}")

    try:
        dynamic_items = load_dynamic_watchlist(today)
        if dynamic_items:
            known = {str(w["code"]) for w in items}
            added = [w for w in dynamic_items if str(w["code"]) not in known]
            if added:
                log(f"[info] watchlist 数据来源：watchlist_dynamic +{len(added)}")
                items.extend(added)
    except Exception as e:
        log(f"[warn] watchlist_dynamic 读取失败：{e}")

    if items:
        return items

    text: str | None = None
    source_hint = ""

    with db_connect(DB) as conn:
        row = conn.execute(
            """SELECT text FROM push_log
               WHERE source='stock-premarket'
                 AND date(timestamp)=date('now','localtime')
               ORDER BY id DESC LIMIT 1""",
        ).fetchone()
    if row:
        text = row[0]
        source_hint = "push_log"
    else:
        # Fallback：data/last_card.md 如果是今天写的就用
        last_card = ROOT / "data" / "last_card.md"
        if last_card.exists():
            mtime = datetime.fromtimestamp(last_card.stat().st_mtime).strftime("%Y-%m-%d")
            if mtime == today:
                text = last_card.read_text(encoding="utf-8")
                source_hint = "last_card.md (未推送)"
                log(f"[info] push_log 无今日 premarket，回退用 last_card.md (mtime={mtime})")

    if text is None:
        log(f"[warn] PREMARKET_MISSING {today} 无 stock-premarket 推送记录、无 last_card.md")
        return []

    log(f"[info] watchlist 数据来源：{source_hint}")

    # 解析逻辑：在派别标题（【派别 X · ...】）下方扫描"N. <6位代码> <名称>"
    items: list[dict] = []
    current_genre = None
    genre_pat = re.compile(r"【派别\s*([ABCD])\s*·")
    code_pat = re.compile(r"^\s*\d+\.\s+(\d{6})\s+([一-龥A-Za-z0-9\-\*]+)")
    buy_pat = re.compile(r"买点[:：]\s*¥?\s*([\d\.]+)")
    stop_pat = re.compile(r"止损[:：]\s*¥?\s*([\d\.]+)")

    current_card: dict | None = None
    for line in text.splitlines():
        m = genre_pat.search(line)
        if m:
            current_genre = m.group(1)
            continue
        m = code_pat.match(line)
        if m:
            if current_card:
                items.append(current_card)
            current_card = {
                "code": m.group(1),
                "name": m.group(2),
                "genre": current_genre or "?",
                "buy": None,
                "stop_loss": None,
            }
            continue
        if current_card is None:
            continue
        m = buy_pat.search(line)
        if m and current_card.get("buy") is None:
            try:
                current_card["buy"] = float(m.group(1))
            except ValueError:
                pass
        m = stop_pat.search(line)
        if m and current_card.get("stop_loss") is None:
            try:
                current_card["stop_loss"] = float(m.group(1))
            except ValueError:
                pass
    if current_card:
        items.append(current_card)
    return items


def load_dynamic_watchlist(today: str) -> list[dict]:
    """把手工 /watch 产生的 watchlist_dynamic 映射到 L2 兼容观察池。

    只有买点和止损完整的记录才能进入交易观察池。
    """
    out: list[dict] = []
    with db_connect(DB) as conn:
        rows = conn.execute(
            """SELECT concept_tag, code, name, role, entry_price, stop_price,
                      target_pct, discipline_type, action_window, status
               FROM watchlist_dynamic
               WHERE trade_date=?
                 AND COALESCE(status, 'pending') IN ('pending', 'triggered')
                 AND entry_price IS NOT NULL
                 AND stop_price IS NOT NULL
               ORDER BY created_at, id""",
            (today,),
        ).fetchall()
    for concept, code, name, role, entry, stop, target, discipline, window, status in rows:
        entry_low = float(entry) if entry is not None else None
        entry_high = entry_low
        max_chase = round(entry_low * 1.025, 2) if entry_low is not None else None
        out.append({
            "code": str(code),
            "name": str(name),
            "genre": discipline or "T",
            "lane": "trend",
            "buy": entry_high,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "max_chase_price": max_chase,
            "stop_loss": float(stop) if stop is not None else None,
            "deadline_time": _dynamic_deadline(window),
            "position_max_pct": 15,
            "target_pct": float(target) if target is not None else None,
            "status": status or "pending",
            "thesis": f"{concept} {role} · 盘中主线确认",
            "source": "watchlist_dynamic",
            "concept": concept,
        })
    return out


def _dynamic_deadline(action_window: str | None) -> str:
    if action_window == "before_1030":
        return "10:30"
    if action_window == "1030_1400":
        return "14:00"
    return "14:30"


def load_holdings() -> list[dict]:
    """转调 stock_codex/domain/holdings.read_holdings，保持 list[dict] 返回兼容下游。"""
    try:
        from stock_codex.domain.holdings import read_holdings  # noqa: WPS433 局部导入避免循环
    except ImportError as e:
        log(f"[warn] lib.holdings 不可用，回退旧读法：{e}")
        if not HOLDINGS_FILE.exists():
            log(f"[warn] holdings.yaml 不存在：{HOLDINGS_FILE}")
            return []
        data = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) or {}
        out = data.get("holdings") or []
        return [h for h in out if h.get("code") and h.get("name") and h.get("cost")]
    holdings = read_holdings()
    # 转 dict，字段名沿用既有约定
    return [
        {
            "code": h.code, "name": h.name, "cost": h.cost, "shares": h.shares,
            "buy_date": h.buy_date.isoformat(), "genre": h.genre,
            "stop_loss": h.stop_loss, "take_profit": h.take_profit,
            "unlock_date": h.unlock_date.isoformat() if h.unlock_date else None,
            "source": h.source, "note": h.note,
        }
        for h in holdings
    ]


_SINA_LINE_PAT = re.compile(r'hq_str_(\w+)="([^"]*)"')


def _sina_code(code: str) -> str:
    """6 位代码 → 新浪格式 sh / sz / bj 前缀。"""
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8")) or code[:3] in ("920", "830"):
        return "bj" + code
    return "sh" + code


def _fetch_spot_sina(codes: list[str]) -> pd.DataFrame:
    """新浪 hq.sinajs.cn 批量直连。绕过东财代理风控。
    字段与 stock_zh_a_spot_em 对齐；新浪不提供量比/换手率，那两列为 None。
    """
    import requests
    if not codes:
        return pd.DataFrame()
    sina_codes = [_sina_code(c) for c in codes]
    url = "https://hq.sinajs.cn/list=" + ",".join(sina_codes)
    r = requests.get(url, headers={"Referer": "https://finance.sina.com.cn/"}, timeout=5)
    r.encoding = "gbk"
    rows = []
    for m in _SINA_LINE_PAT.finditer(r.text):
        sina_code = m.group(1)
        f = m.group(2).split(",")
        if len(f) < 10 or not f[0]:  # 停牌/空响应：name 空
            continue
        code = sina_code[2:]
        try:
            prev_close = float(f[2]) if f[2] else 0
            cur = float(f[3]) if f[3] else prev_close
        except ValueError:
            continue
        pct = round((cur - prev_close) / prev_close * 100, 2) if prev_close else 0
        def _f(s):
            try:
                return float(s)
            except (ValueError, TypeError):
                return None
        rows.append({
            "代码": code,
            "名称": f[0],
            "最新价": cur,
            "涨跌幅": pct,
            "最高": _f(f[4]),
            "最低": _f(f[5]),
            "今开": _f(f[1]),
            "量比": None,       # 新浪 hq 不提供
            "换手率": None,     # 新浪 hq 不提供
            "成交额": _f(f[9]),
        })
    return pd.DataFrame(rows)


def fetch_spot(codes: list[str]) -> pd.DataFrame:
    """优先批量 EM spot；失败回退到新浪 hq 批量。
    旧版的 stock_bid_ask_em 逐只回退已废弃——同走东财代理，同样会被风控拒。
    """
    import time
    if not codes:
        return pd.DataFrame()
    cols_keep = ["代码", "名称", "最新价", "涨跌幅", "最高", "最低", "今开", "量比", "换手率", "成交额"]
    for attempt in range(2):
        try:
            df = ak.stock_zh_a_spot_em()
            df = df[df["代码"].isin(codes)][cols_keep].copy()
            return df.reset_index(drop=True)
        except Exception as e:
            log(f"[warn] EM 批量 spot 第 {attempt+1} 次失败: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(1.5)

    log("[warn] EM 全失败，回退到新浪 hq")
    try:
        df = _fetch_spot_sina(codes)
        if not df.empty:
            log(f"[ok] 新浪 hq 拉到 {len(df)} 只")
            return df
        log("[warn] 新浪 hq 返回空")
    except Exception as e:
        log(f"[warn] 新浪 hq 失败: {type(e).__name__}: {str(e)[:120]}")
    return pd.DataFrame()


def fetch_zt_pool_today() -> pd.DataFrame:
    """今日涨停池（11:30 / 14:30 用）。盘中可能未封板的票还没进池，这是已知限制。"""
    today = datetime.now().strftime("%Y%m%d")
    return _fetch_structured_table(
        label="涨停池",
        cache_name="zt_pool",
        fetcher=lambda: ak.stock_zt_pool_em(date=today),
    )


def fetch_zbgc_today() -> pd.DataFrame:
    """今日炸板池。"""
    today = datetime.now().strftime("%Y%m%d")
    return _fetch_structured_table(
        label="炸板池",
        cache_name="zbgc_pool",
        fetcher=lambda: ak.stock_zt_pool_zbgc_em(date=today),
    )


def fetch_concept_hot() -> pd.DataFrame:
    """同花顺概念板块当日涨幅 Top 10。"""
    def _fetch() -> pd.DataFrame:
        df = ak.stock_board_concept_name_ths()
        if "涨跌幅" in df.columns:
            return df.sort_values("涨跌幅", ascending=False).head(15)

        # AkShare 近期将同花顺接口降级为仅返回 name/code；此时改走东财实时榜，
        # 保住 11:30 / 14:30 对概念热度排序和涨跌幅解释的硬依赖。
        em_df = ak.stock_board_concept_name_em()
        if "涨跌幅" in em_df.columns:
            return em_df.sort_values("涨跌幅", ascending=False).head(15)
        raise ValueError("同花顺和东财概念榜均缺少涨跌幅字段")

    return _fetch_structured_table(
        label="概念热度",
        cache_name="concept_hot",
        fetcher=_fetch,
        validator=lambda df: "涨跌幅" in df.columns,
    )


def _cache_path(cache_name: str) -> Path:
    return CACHE_DIR / f"{datetime.now().strftime('%Y%m%d')}_{cache_name}.json"


def _write_df_cache(cache_name: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "cached_at": datetime.now().replace(microsecond=0).isoformat(),
        "data": json.loads(df.to_json(orient="split", force_ascii=False, date_format="iso")),
    }
    _cache_path(cache_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_df_cache(cache_name: str) -> tuple[pd.DataFrame, str | None]:
    path = _cache_path(cache_name)
    if not path.exists():
        return pd.DataFrame(), None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        data = payload.get("data") or {}
        df = pd.DataFrame(data.get("data") or [], columns=data.get("columns") or [])
        cached_at = payload.get("cached_at")
        if not df.empty:
            df.attrs["snapshot_source"] = "cache"
            if cached_at:
                df.attrs["snapshot_at"] = str(cached_at)
        return df, cached_at
    except Exception as e:
        log(f"[warn] 缓存读取失败 {path.name}: {type(e).__name__}: {str(e)[:120]}")
        return pd.DataFrame(), None


def _fetch_structured_table(
    *,
    label: str,
    cache_name: str,
    fetcher,
    attempts: int = 3,
    sleep_seconds: float = 1.5,
    validator=None,
) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            df = fetcher()
            if not isinstance(df, pd.DataFrame):
                raise TypeError(f"{label} 返回非 DataFrame: {type(df).__name__}")
            if validator is not None and not validator(df):
                raise ValueError(f"{label} 返回结构不可用")
            if not df.empty:
                _write_df_cache(cache_name, df)
            return df
        except Exception as e:
            last_error = e
            log(f"[warn] {label} 第 {attempt}/{attempts} 次拉取失败: {type(e).__name__}: {str(e)[:120]}")
            if attempt < attempts:
                time.sleep(sleep_seconds)

    cached, cached_at = _read_df_cache(cache_name)
    if validator is not None and not validator(cached):
        cached = pd.DataFrame()
    if not cached.empty:
        log(f"[warn] {label} 使用缓存快照 {cached_at}（实时源失败: {last_error}）")
        return cached

    log(f"[warn] {label} 拉取失败且无可用缓存: {last_error}")
    return pd.DataFrame()


def _cache_note(df: pd.DataFrame) -> str | None:
    if df.attrs.get("snapshot_source") != "cache":
        return None
    snapshot_at = df.attrs.get("snapshot_at") or "unknown"
    return f"（使用缓存快照：{snapshot_at}，实时源失败）"


def load_latest_market_snapshot(now: datetime) -> dict:
    """读取 theme loop 最近一次紧凑市场快照；表未初始化时返回空。"""
    try:
        with db_connect(DB) as conn:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_snapshot'"
            ).fetchone()
            if not has_table:
                return {}
            row = conn.execute(
                """SELECT payload_json FROM market_snapshot
                   WHERE trade_date=? AND snapshot_ts<=?
                   ORDER BY snapshot_ts DESC LIMIT 1""",
                (now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds")),
            ).fetchone()
        if not row:
            return {}
        payload = json.loads(row[0])
        snapshot_at = str(payload.get("snapshot_ts") or "")
        try:
            age_seconds = max(0, int((now - datetime.fromisoformat(snapshot_at)).total_seconds()))
        except ValueError:
            age_seconds = None
        payload["age_seconds"] = age_seconds
        if age_seconds is None or age_seconds > MARKET_SNAPSHOT_STALE_SECONDS:
            payload["is_stale"] = True
        return payload
    except Exception as e:
        log(f"[warn] market_snapshot 读取失败：{type(e).__name__}: {str(e)[:120]}")
        return {}


def _build_allowed(
    *, watchlist: list[dict], holdings: list[dict], spot,
    zt, zb, cc, now: datetime, label: str, market_snapshot: dict | None = None,
) -> dict:
    """聚合本次 fact pack 的全部允许引用事实。卡片里任何不在此清单的数据点 → 违规。"""
    codes: dict[str, str] = {}
    lianban: dict[str, int] = {}
    pct: dict[str, float] = {}
    concepts: list[str] = []

    for w in watchlist:
        codes[str(w["code"])] = str(w["name"])
    for h in holdings:
        codes[str(h["code"])] = str(h["name"])

    if spot is not None and not spot.empty:
        for _, r in spot.iterrows():
            code = str(r.get("代码") or r.get("code") or "")
            name = str(r.get("名称") or r.get("name") or "")
            if not code or len(code) != 6:
                continue
            codes[code] = name or codes.get(code, name)
            try:
                pct[code] = round(float(r.get("涨跌幅")), 2)
            except (TypeError, ValueError):
                pass

    if zt is not None and not zt.empty:
        for _, r in zt.iterrows():
            code = str(r.get("代码") or "")
            name = str(r.get("名称") or "")
            if not code or len(code) != 6:
                continue
            codes[code] = name or codes.get(code, name)
            try:
                lianban[code] = int(r.get("连板数"))
            except (TypeError, ValueError):
                pass
            try:
                pct[code] = round(float(r.get("涨跌幅")), 2)
            except (TypeError, ValueError):
                pass

    if zb is not None and not zb.empty:
        for _, r in zb.iterrows():
            code = str(r.get("代码") or "")
            name = str(r.get("名称") or "")
            if not code or len(code) != 6:
                continue
            codes[code] = name or codes.get(code, name)
            try:
                pct[code] = round(float(r.get("涨跌幅")), 2)
            except (TypeError, ValueError):
                pass

    if cc is not None and not cc.empty:
        if "概念名称" in cc.columns:
            concepts = [str(x) for x in cc["概念名称"].tolist()[:30]]
        elif "板块名称" in cc.columns:
            concepts = [str(x) for x in cc["板块名称"].tolist()[:30]]
        elif "name" in cc.columns:
            concepts = [str(x) for x in cc["name"].tolist()[:30]]

    summary: dict[str, object] = {
        "date": now.strftime("%Y-%m-%d"),
    }
    if zt is not None and not zt.empty:
        summary["limit_up"] = int(len(zt))
        if zt.attrs.get("snapshot_source") == "cache":
            summary["limit_up_snapshot_source"] = "cache"
            summary["limit_up_snapshot_at"] = str(zt.attrs.get("snapshot_at") or "")
    if zb is not None and not zb.empty:
        summary["broken"] = int(len(zb))
        if zb.attrs.get("snapshot_source") == "cache":
            summary["broken_snapshot_source"] = "cache"
            summary["broken_snapshot_at"] = str(zb.attrs.get("snapshot_at") or "")
    if cc is not None and not cc.empty and cc.attrs.get("snapshot_source") == "cache":
        summary["concept_snapshot_source"] = "cache"
        summary["concept_snapshot_at"] = str(cc.attrs.get("snapshot_at") or "")

    market_snapshot = market_snapshot or {}
    market_snapshot_stale = not market_snapshot or bool(market_snapshot.get("is_stale"))
    summary["market_snapshot_stale"] = market_snapshot_stale
    if market_snapshot.get("snapshot_ts"):
        summary["market_snapshot_at"] = str(market_snapshot["snapshot_ts"])
    narrative_snapshot = {} if market_snapshot_stale else market_snapshot
    pool_summary = dict(narrative_snapshot.get("pool_summary") or {})
    if zt is not None and not zt.empty:
        pool_summary["limit_up"] = int(len(zt))
    if zb is not None and not zb.empty:
        pool_summary["broken"] = int(len(zb))

    return {
        "schema_version": "2",
        "skill": "stock-intraday",
        "label": label,
        "snapshot_at": now.replace(microsecond=0).isoformat(),
        "codes": codes,
        "lianban": lianban,
        "pct": pct,
        "summary": summary,
        "concepts": concepts,
        "news": narrative_snapshot.get("news") or [],
        "global_markets": narrative_snapshot.get("overseas") or {},
        "market_breadth": narrative_snapshot.get("breadth") or {},
        "indices": narrative_snapshot.get("indices") or {},
        "turnover": narrative_snapshot.get("turnover") or {},
        "theme_strength": narrative_snapshot.get("theme_strength") or {},
        "overseas": narrative_snapshot.get("overseas") or {},
        "anchors": narrative_snapshot.get("anchors") or {},
        "pool_summary": pool_summary,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--halfday", action="store_true", help="11:30 半日分支")
    p.add_argument("--endday", action="store_true", help="14:30 尾盘分支")
    args = p.parse_args()

    now = datetime.now()
    print(f"=== 盘中实时拉取 · {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    section("一、今日决策单 / 观察池")
    watchlist = load_today_watchlist()
    if not watchlist:
        print("（今日无 L1 盘前推送记录，跳过观察池）")
    else:
        for w in watchlist:
            print(
                f"  [{w['genre']}] {w['code']} {w['name']}  "
                f"买点={w.get('buy')}  止损={w.get('stop_loss')}"
            )

    section("二、实盘持仓（holdings.yaml）")
    holdings = load_holdings()
    if not holdings:
        print("（今日空仓 / holdings.yaml 无条目）")
    else:
        for h in holdings:
            print(
                f"  [{h.get('genre','?')}] {h['code']} {h['name']}  "
                f"成本={h['cost']}  止损={h.get('stop_loss')}  止盈={h.get('take_profit')}  "
                f"股数={h.get('shares')}  备注={h.get('note','')}"
            )

    section("三、观察池 + 持仓实时行情")
    all_codes = list({w["code"] for w in watchlist} | {h["code"] for h in holdings})
    spot = fetch_spot(all_codes)
    if spot.empty:
        print("（无标的或行情拉取失败）")
    else:
        print(spot.to_string(index=False))

    zt = pd.DataFrame()
    zb = pd.DataFrame()
    cc = pd.DataFrame()
    label = "纪律分支"
    if args.halfday or args.endday:
        label = "半日（11:30）" if args.halfday else "尾盘快照（14:30）"
        section(f"四、{label}涨停结构")
        zt = fetch_zt_pool_today()
        note = _cache_note(zt)
        if note:
            print(note)
        if zt.empty:
            print("（涨停池暂无数据）")
        else:
            print(f"涨停数: {len(zt)}")
            if "连板数" in zt.columns:
                print("\n连板梯队 Top 15:")
                print(
                    zt.sort_values("连板数", ascending=False)
                    .head(15)[["代码", "名称", "连板数", "涨跌幅", "封板资金", "所属行业"]]
                    .to_string(index=False)
                )

        section(f"五、{label}炸板池")
        zb = fetch_zbgc_today()
        note = _cache_note(zb)
        if note:
            print(note)
        if zb.empty:
            print("（炸板池暂无数据）")
        else:
            print(f"炸板数: {len(zb)}")
            cols = [c for c in ["代码", "名称", "涨跌幅", "炸板次数", "所属行业"] if c in zb.columns]
            print(zb[cols].head(15).to_string(index=False))

        section(f"六、{label}概念热度 Top 15")
        cc = fetch_concept_hot()
        note = _cache_note(cc)
        if note:
            print(note)
        if cc.empty:
            print("（概念热度暂无数据）")
        else:
            cols = [c for c in ["概念名称", "板块名称", "涨跌幅", "上涨家数", "下跌家数", "领涨股", "领涨股票", "领涨股-涨跌幅", "领涨股票-涨跌幅"] if c in cc.columns]
            print(cc[cols].to_string(index=False))

    print("\n=== fetch_realtime done ===")

    # ALLOWED 段：卡片校验的唯一事实清单（见 docs/allowed_schema.md）
    import json
    allowed = _build_allowed(
        watchlist=watchlist, holdings=holdings, spot=spot,
        zt=zt, zb=zb, cc=cc, now=now, label=label,
        market_snapshot=load_latest_market_snapshot(now),
    )
    print("\n=== ALLOWED ===")
    print(json.dumps(allowed, ensure_ascii=False, indent=2))
    print("=== /ALLOWED ===")

    # 同时落盘，供 push.py 校验时读取
    out = ROOT / "data" / "allowed_latest_stock-intraday.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(allowed, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

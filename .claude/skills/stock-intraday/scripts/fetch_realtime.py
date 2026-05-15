"""
盘中实时数据拉取：观察池 + 持仓 + 实时行情。

用法：
    python fetch_realtime.py              # 纪律分支（09:30 / 09:45）
    python fetch_realtime.py --halfday    # 11:30 半日分支，额外拉涨停结构 + 概念热度
    python fetch_realtime.py --endday     # 14:30 尾盘分支，全日数据
"""

from __future__ import annotations
import argparse
import re
import sqlite3
import sys
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

sys.path.insert(0, str(ROOT / "code"))
from db import connect as db_connect  # noqa: E402


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def section(title: str):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def load_today_watchlist() -> list[dict]:
    """从 push_log 取今日 stock-premarket 最新一条，正则提取代码 + 名称 + 派别 + 买卖纪律。

    Fallback 链：
    1. push_log 今日 stock-premarket（已推送）
    2. data/last_card.md（mtime=今天，已写卡未推送，比如 push 链路慢）
    3. 都没有 → 返回空，并打 PREMARKET_MISSING 标记给上游 SKILL 走兜底文案
    """
    today = datetime.now().strftime("%Y-%m-%d")
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


def load_holdings() -> list[dict]:
    """转调 code/lib/holdings.read_holdings，保持 list[dict] 返回兼容下游。"""
    try:
        from lib.holdings import read_holdings  # noqa: WPS433 局部导入避免循环
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
    try:
        today = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zt_pool_em(date=today)
        return df
    except Exception as e:
        log(f"[warn] 涨停池拉取失败: {e}")
        return pd.DataFrame()


def fetch_zbgc_today() -> pd.DataFrame:
    """今日炸板池。"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        return ak.stock_zt_pool_zbgc_em(date=today)
    except Exception as e:
        log(f"[warn] 炸板池拉取失败: {e}")
        return pd.DataFrame()


def fetch_concept_hot() -> pd.DataFrame:
    """同花顺概念板块当日涨幅 Top 10。"""
    try:
        df = ak.stock_board_concept_name_ths()
        if "涨跌幅" in df.columns:
            df = df.sort_values("涨跌幅", ascending=False).head(15)
        return df
    except Exception as e:
        log(f"[warn] 概念板块拉取失败: {e}")
        return pd.DataFrame()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--halfday", action="store_true", help="11:30 半日分支")
    p.add_argument("--endday", action="store_true", help="14:30 尾盘分支")
    args = p.parse_args()

    now = datetime.now()
    print(f"=== 盘中实时拉取 · {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    section("一、今日观察池（解析自 push_log）")
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

    if args.halfday or args.endday:
        label = "半日（11:30）" if args.halfday else "全日（14:30）"
        section(f"四、{label}涨停结构")
        zt = fetch_zt_pool_today()
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
        if zb.empty:
            print("（炸板池暂无数据）")
        else:
            print(f"炸板数: {len(zb)}")
            cols = [c for c in ["代码", "名称", "涨跌幅", "炸板次数", "所属行业"] if c in zb.columns]
            print(zb[cols].head(15).to_string(index=False))

        section(f"六、{label}概念热度 Top 15")
        cc = fetch_concept_hot()
        if cc.empty:
            print("（概念热度暂无数据）")
        else:
            cols = [c for c in ["概念名称", "涨跌幅", "上涨家数", "领涨股", "领涨股-涨跌幅"] if c in cc.columns]
            print(cc[cols].to_string(index=False))

    print("\n=== fetch_realtime done ===")


if __name__ == "__main__":
    main()

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
    """从 push_log 取今日 stock-premarket 最新一条，正则提取代码 + 名称 + 派别 + 买卖纪律。"""
    today = datetime.now().strftime("%Y-%m-%d")
    with db_connect(DB) as conn:
        row = conn.execute(
            """SELECT text FROM push_log
               WHERE source='stock-premarket'
                 AND date(timestamp)=date('now','localtime')
               ORDER BY id DESC LIMIT 1""",
        ).fetchone()
    if not row:
        log(f"[warn] {today} 无 stock-premarket 推送记录")
        return []
    text = row[0]

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
    if not HOLDINGS_FILE.exists():
        log(f"[warn] holdings.yaml 不存在：{HOLDINGS_FILE}")
        return []
    data = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) or {}
    out = data.get("holdings") or []
    return [h for h in out if h.get("code") and h.get("name") and h.get("cost")]


def fetch_spot(codes: list[str]) -> pd.DataFrame:
    """优先批量 stock_zh_a_spot_em（含 5000+ 票），失败回退到逐只 stock_bid_ask_em。"""
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
            log(f"[warn] 批量 spot 第 {attempt+1} 次失败: {e}")
            time.sleep(1.5)

    log("[warn] 批量失败，回退到逐只 stock_bid_ask_em")
    rows = []
    for code in codes:
        try:
            bid = ak.stock_bid_ask_em(symbol=code)
            d = dict(zip(bid["item"], bid["value"]))
            rows.append({
                "代码": code,
                "名称": d.get("股票简称", ""),
                "最新价": d.get("最新"),
                "涨跌幅": d.get("涨幅"),
                "最高": d.get("最高"),
                "最低": d.get("最低"),
                "今开": d.get("今开"),
                "量比": d.get("量比"),
                "换手率": d.get("换手"),
                "成交额": d.get("成交额"),
            })
        except Exception as e:
            log(f"[warn] {code} 单只拉取失败: {e}")
    return pd.DataFrame(rows)


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

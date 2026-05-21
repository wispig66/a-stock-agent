"""
扩展数据源 · 抽取自 https://github.com/simonlin1212/a-stock-data (Apache License 2.0)
作者：Simon 林（抖音「Simon林」/ 公众号「硅基世纪」）

本文件保留 attribution。lockup_expiry 的 upcoming 部分有改动：
原版用 stock_restricted_release_detail_em(date=X)（单日全市场）查未来 90 天，
逻辑不通；这里改用 stock_restricted_release_queue_em(symbol=code) 返回该票
全部历史+未来后按日期切。

5 个端点（外加 CLI 探测模式）：
- ths_hot_reason            同花顺热点 + 题材归因（盘后 15:30+ 有效）
- daily_dragon_tiger        全市场龙虎榜（东财 datacenter）
- lockup_expiry             解禁日历（90 天预警）
- mootdx_quote / mootdx_bars 实时行情 + K 线（TCP 7709，国内 IP）
- baidu_fund_flow_realtime / baidu_fund_flow_history  百度分钟级/日级资金流

CLI 模式（用于 SKILL 里 LLM 直接调）：
    python extras.py --hot                     拉今天/上交易日的同花顺热点
    python extras.py --lhb                     拉今天的全市场龙虎榜
    python extras.py --lockup 600519           查单只票解禁
    python extras.py --quote 600519            查实时价
    python extras.py --flow 600519             查 20 日资金流
"""

from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"
)


# ============================================================
# 1. 同花顺热点 reason tags
# ============================================================

def ths_hot_reason(date: str = None) -> pd.DataFrame:
    """同花顺当日强势股 + 题材归因。

    date: 'YYYY-MM-DD'，None=今天。盘后 15:30+ 才有当日数据，盘前调要传 D-1。
    返回 DataFrame，含「代码 / 名称 / 题材归因 / 涨幅% / 换手率% / 成交额 / 大单净量」。
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    url = (
        f"http://zx.10jqka.com.cn/event/api/getharden/"
        f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
    )
    headers = {"User-Agent": _DEFAULT_UA}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("errocode", 0) != 0:
        raise RuntimeError(f"同花顺热点错误: {data.get('errormsg', '')}")

    rows = data.get("data") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    rename_map = {
        "name": "名称", "code": "代码", "reason": "题材归因",
        "close": "收盘价", "zhangdie": "涨跌额", "zhangfu": "涨幅%",
        "huanshou": "换手率%", "chengjiaoe": "成交额",
        "chengjiaoliang": "成交量", "ddejingliang": "大单净量",
        "market": "市场",
    }
    df = df.rename(columns=rename_map)
    keep = [c for c in ["代码", "名称", "题材归因", "涨幅%", "换手率%",
                        "成交额", "大单净量", "收盘价"] if c in df.columns]
    return df[keep] if keep else df


# ============================================================
# 2. 全市场龙虎榜（东财 datacenter，比 stock_lhb_detail_em 稳）
# ============================================================

def daily_dragon_tiger(trade_date: str = None,
                      min_net_buy: float = None) -> dict:
    """全市场龙虎榜。
    trade_date: 'YYYY-MM-DD'，None=今天
    min_net_buy: 净买入下限（万元），None 不过滤
    返回 {date, total_records, stocks: [...]}
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "ALL",
        "filter": f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')",
        "pageNumber": "1",
        "pageSize": "500",
        "sortTypes": "-1",
        "sortColumns": "BILLBOARD_NET_AMT",
        "source": "WEB",
        "client": "WEB",
    }
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Referer": "https://data.eastmoney.com/",
    }
    r = requests.get(url, params=params, headers=headers, timeout=15)
    d = r.json()
    if not d.get("success") or not d.get("result") or not d["result"].get("data"):
        return {"date": trade_date, "total_records": 0, "stocks": [],
                "note": "无数据（非交易日或盘后未更新）"}

    data = d["result"]["data"]
    actual_date = data[0].get("TRADE_DATE", "")[:10] if data else trade_date
    stocks = []
    for row in data:
        net_buy = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
        if min_net_buy is not None and net_buy < min_net_buy:
            continue
        stocks.append({
            "code": row.get("SECURITY_CODE", ""),
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "reason": row.get("EXPLANATION", ""),
            "close": row.get("CLOSE_PRICE") or 0,
            "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
            "net_buy_wan": round(net_buy, 1),
            "buy_wan": round((row.get("BILLBOARD_BUY_AMT") or 0) / 10000, 1),
            "sell_wan": round((row.get("BILLBOARD_SELL_AMT") or 0) / 10000, 1),
            "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2),
        })
    return {"date": actual_date, "total_records": len(stocks), "stocks": stocks}


# ============================================================
# 3. 解禁日历（90 天预警）
# ============================================================

def lockup_expiry(code: str, trade_date: str = None,
                  forward_days: int = 90) -> dict:
    """单只票解禁历史 + 未来 N 天预警。

    用 akshare 的 stock_restricted_release_queue_em(symbol=code)，
    它返回该票全部历史+未来，再按 trade_date 切分。
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    base = datetime.strptime(trade_date, "%Y-%m-%d")
    forward_end = base + timedelta(days=forward_days)

    import akshare as ak
    history, upcoming = [], []
    try:
        df = ak.stock_restricted_release_queue_em(symbol=code)
    except Exception as e:
        return {"history": [], "upcoming": [],
                "error": f"akshare 失败: {type(e).__name__}: {str(e)[:80]}"}

    if df is None or df.empty:
        return {"history": [], "upcoming": [], "note": "无解禁记录"}

    # akshare 列名（新版）: 解禁时间 / 限售股类型 / 解禁数量 / 实际解禁数量 / 占总市值比例 / 占流通市值比例 / 占流通A股比例 等
    date_col = next((c for c in ["解禁时间", "解禁日期"] if c in df.columns), None)
    type_col = next((c for c in ["限售股类型", "类型"] if c in df.columns), None)
    shares_col = next((c for c in ["解禁数量", "实际解禁数量"] if c in df.columns), None)
    ratio_col = next((c for c in ["占流通市值比例", "占总市值比例",
                                  "占流通A股比例", "占流通股比例",
                                  "实际解禁市值占总市值比例"] if c in df.columns), None)
    if not date_col:
        return {"history": [], "upcoming": [],
                "error": f"列名不识别: {list(df.columns)[:8]}"}

    for _, row in df.iterrows():
        try:
            d_str = str(row.get(date_col, ""))[:10]
            d = datetime.strptime(d_str, "%Y-%m-%d")
        except Exception:
            continue
        item = {
            "date": d_str,
            "type": str(row.get(type_col, "")) if type_col else "",
            "shares": row.get(shares_col, 0) if shares_col else 0,
            "float_ratio": float(row.get(ratio_col, 0) or 0) if ratio_col else 0,
        }
        if d < base:
            history.append(item)
        elif d <= forward_end:
            upcoming.append(item)

    history = sorted(history, key=lambda x: x["date"], reverse=True)[:10]
    upcoming = sorted(upcoming, key=lambda x: x["date"])
    return {"history": history, "upcoming": upcoming}


# ============================================================
# 4. mootdx 行情 + K 线
# ============================================================

_mootdx_client = None

def _get_mootdx():
    global _mootdx_client
    if _mootdx_client is None:
        from mootdx.quotes import Quotes  # 延迟导入，避免无网环境 import 失败
        _mootdx_client = Quotes.factory(market="std")
    return _mootdx_client


def mootdx_quote(symbol: str) -> dict:
    """实时报价（46 字段）。返回扁平 dict。"""
    cli = _get_mootdx()
    df = cli.quotes(symbol=[symbol])
    if df is None or len(df) == 0:
        return {}
    row = df.iloc[0].to_dict()
    # 关键字段筛选
    keep = {k: row.get(k) for k in [
        "code", "price", "open", "high", "low", "last_close",
        "vol", "amount", "servertime",
        "bid1", "ask1", "bid_vol1", "ask_vol1"
    ] if k in row}
    return keep


def mootdx_bars(symbol: str, category: int = 4, offset: int = 30) -> pd.DataFrame:
    """K 线。category: 4=日 5=周 6=月 7=1min 8=5min 9=15min 10=30min 11=60min"""
    cli = _get_mootdx()
    return cli.bars(symbol=symbol, category=category, offset=offset)


# ============================================================
# 5. 百度资金流（分钟级 + 20 日历史）
# ============================================================

_BAIDU_PAE_HEADERS = {
    "Host": "finance.pae.baidu.com",
    "User-Agent": _DEFAULT_UA,
    "Accept": "application/vnd.finance-web.v1+json",
    "Origin": "https://gushitong.baidu.com",
    "Referer": "https://gushitong.baidu.com/",
}


def baidu_fund_flow_realtime(code: str, date: str = None) -> list[dict]:
    """分钟级资金流（262 timepoints 09:10-15:00）。
    date: YYYYMMDD 紧凑格式，None=今天
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    url = (f"https://finance.pae.baidu.com/vapi/v1/fundflow"
           f"?code={code}&market=ab&date={date}&finClientType=pc")
    r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
    d = r.json()
    if str(d.get("ResultCode", -1)) != "0":
        return []
    raw = d.get("Result", {}).get("update_data", "")
    if not raw:
        return []
    rows = []
    for segment in raw.split(";"):
        parts = segment.split(",")
        if len(parts) >= 9:
            rows.append({
                "time": parts[0],
                "mainForce": float(parts[2]) if parts[2] else 0,
                "retail": float(parts[3]) if parts[3] else 0,
                "super": float(parts[4]) if parts[4] else 0,
                "large": float(parts[5]) if parts[5] else 0,
                "price": float(parts[8]) if parts[8] else 0,
            })
    return rows


def baidu_fund_flow_history(code: str, days: int = 20) -> list[dict]:
    """日级资金流历史（最近 N 个交易日）。"""
    url = (f"https://finance.pae.baidu.com/vapi/v1/fundsortlist"
           f"?code={code}&market=ab&pn=0&rn={days}&finClientType=pc")
    r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
    d = r.json()
    if str(d.get("ResultCode", -1)) != "0":
        return []
    rows = []
    for item in d.get("Result", {}).get("list", []):
        rows.append({
            "date": item.get("showtime", ""),
            "close": item.get("closepx", ""),
            "change_pct": item.get("ratio", ""),
            "superNetIn": item.get("superNetIn", ""),
            "largeNetIn": item.get("largeNetIn", ""),
            "mediumNetIn": item.get("mediumNetIn", ""),
            "littleNetIn": item.get("littleNetIn", ""),
            "mainIn": item.get("extMainIn", ""),
        })
    return rows


# ============================================================
# CLI（给 SKILL 里 LLM 直接 shell 调）
# ============================================================

def _pretty(obj):
    if isinstance(obj, pd.DataFrame):
        return obj.to_string(index=False)
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hot", nargs="?", const="auto",
                   help="同花顺热点 reason tags。可选传 YYYY-MM-DD")
    p.add_argument("--lhb", nargs="?", const="auto",
                   help="全市场龙虎榜。可选传 YYYY-MM-DD")
    p.add_argument("--lockup", help="解禁查询，传股票代码（6 位）")
    p.add_argument("--quote", help="mootdx 实时报价，传股票代码")
    p.add_argument("--flow", help="百度日级资金流，传股票代码")
    p.add_argument("--days", type=int, default=20, help="资金流回看天数")
    args = p.parse_args()

    if args.hot is not None:
        d = None if args.hot == "auto" else args.hot
        df = ths_hot_reason(date=d)
        print(_pretty(df.head(50) if not df.empty else df))
        return

    if args.lhb is not None:
        d = None if args.lhb == "auto" else args.lhb
        print(_pretty(daily_dragon_tiger(trade_date=d)))
        return

    if args.lockup:
        print(_pretty(lockup_expiry(args.lockup)))
        return

    if args.quote:
        print(_pretty(mootdx_quote(args.quote)))
        return

    if args.flow:
        print(_pretty(baidu_fund_flow_history(args.flow, days=args.days)))
        return

    p.print_help()


if __name__ == "__main__":
    main()

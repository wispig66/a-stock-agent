"""
全市场异动捕捉 daemon · 独立后台进程，不调 CC。

差异化定位：
  watch_loop.py — 盯今日观察池 + holdings（已知名单）
  anomaly_loop.py — 扫全市场新冒头的票（未知名单），自动排除已在监控的

数据源：akshare stock_changes_em（东财异动池，多 symbol）
监控时段：交易日 09:31-11:29 + 13:01-14:59
轮询频率：90 秒（与 watch_loop 错开 ±10s）

扫描的 anomaly symbol（每轮全部扫一遍）：
  · 火箭发射 — 3 分钟急涨 ≥ 5%
  · 封涨停 — 新封板
  · 涨停打开 — 炸板（情绪杀器）
  · 60日新高 — 中线突破

去重：(code, kind) 一会话只推一次；新一轮只看 时间 > 上轮最大时间 的条目。
过滤：观察池 + holdings 中的代码不推（那些 watch_loop 在管）。

用法：
  python anomaly_loop.py
  python anomaly_loop.py --once
  python anomaly_loop.py --interval 60
"""

from __future__ import annotations
import argparse
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "stock-intraday" / "scripts"))

from fetch_realtime import load_today_watchlist, load_holdings  # noqa: E402
from notify import push  # noqa: E402

SESSION_AM = (dtime(9, 31), dtime(11, 29))
SESSION_PM = (dtime(13, 1), dtime(14, 59))

SYMBOLS = {
    "火箭发射": "🚀 急涨",
    "封涨停板": "✅ 封板",
    "打开涨停板": "💥 炸板",
    "60日新高": "📈 60日新高",
}


def in_session(now: datetime) -> bool:
    t = now.time()
    return SESSION_AM[0] <= t <= SESSION_AM[1] or SESSION_PM[0] <= t <= SESSION_PM[1]


def fetch_anomaly(symbol: str):
    import akshare as ak
    return ak.stock_changes_em(symbol=symbol)


def fmt_time(t) -> str:
    if t is None or t == "":
        return ""
    try:
        return t.strftime("%H:%M:%S")
    except AttributeError:
        return str(t)


def format_alert(now: datetime, symbol: str, label: str, row: dict) -> str:
    code = row["代码"]
    name = row["名称"]
    info = row.get("相关信息", "")
    t = fmt_time(row.get("时间")) or now.strftime("%H:%M:%S")
    return f"🆕 [{t[:5]}] {label} · {code} {name} · {symbol}  {info}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=90)
    args = p.parse_args()

    excluded = {w["code"] for w in load_today_watchlist()} | {h["code"] for h in load_holdings()}
    print(f"[anomaly_loop] 排除已监控票 {len(excluded)} 只（观察池+持仓）", flush=True)

    sent: set[tuple[str, str]] = set()
    last_max_time: dict[str, str] = {sym: "" for sym in SYMBOLS}
    round_idx = 0

    while True:
        now = datetime.now()
        if not in_session(now):
            if args.once:
                print("[anomaly_loop] 非交易时段 + --once，退出")
                return
            if now.time() > dtime(15, 0):
                print("[anomaly_loop] 已过 15:00，结束监控")
                return
            print(f"[anomaly_loop] {now.strftime('%H:%M:%S')} 非交易时段，等待…", flush=True)
            time.sleep(args.interval)
            continue

        round_idx += 1
        total_new = 0
        for symbol, label in SYMBOLS.items():
            try:
                df = fetch_anomaly(symbol)
            except Exception as e:
                print(f"[anomaly_loop] round {round_idx} {symbol} fetch 失败: {e}", flush=True)
                continue
            if df is None or df.empty:
                continue

            new_max = last_max_time[symbol]
            for _, row in df.iterrows():
                code = row["代码"]
                t = fmt_time(row.get("时间"))
                if code in excluded:
                    continue
                if t <= last_max_time[symbol]:
                    continue
                key = (code, symbol)
                if key in sent:
                    if t > new_max:
                        new_max = t
                    continue
                sent.add(key)
                total_new += 1
                msg = format_alert(now, symbol, label, row.to_dict())
                try:
                    r = push(msg, source="stock-anomaly")
                    print(f"[anomaly_loop] PUSH {code} {symbol} msg_id={r['result']['message_id']}", flush=True)
                except Exception as e:
                    print(f"[anomaly_loop] push 失败 {code} {symbol}: {e}", flush=True)
                if t > new_max:
                    new_max = t
            last_max_time[symbol] = new_max

        print(f"[anomaly_loop] round {round_idx} {now.strftime('%H:%M:%S')} 新增 {total_new} 条", flush=True)

        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

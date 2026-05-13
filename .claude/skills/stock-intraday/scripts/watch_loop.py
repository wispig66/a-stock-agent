"""
盘中阈值轮询脚本 · 独立后台进程，不调 CC。

监控对象：今日 push_log 的观察池 + holdings.yaml 持仓
监控时段：交易日 09:31-11:29 + 13:01-14:59（避开 4 时点和 CC 卡片重叠）
轮询频率：90 秒一次
触发条件（每只票同一类型一次会话只推一次）：
  · 💥 跌破止损：current ≤ holdings.stop_loss → 强推
  · 🚨 跌破观察池止损：current ≤ watchlist.stop_loss
  · 🚀 触发观察池买点：current ≥ watchlist.buy AND 当前涨幅 < 9.8（未涨停）
  · ✅ 封板：涨幅 ≥ 9.8（注意不是 10，主板涨停 10%，创业板 20%——这里只盯主板）
  · 💥 持仓异动放量：持仓股 |涨幅| ≥ 5 且 量比 ≥ 2
  · 💥 持仓砸盘：持仓股涨幅 ≤ -5

用法：
  python watch_loop.py                  # 持续运行直到 15:00
  python watch_loop.py --once           # 跑一轮就退（测试用）
  python watch_loop.py --interval 60    # 自定义轮询秒数（默认 90）
"""

from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_realtime import load_today_watchlist, load_holdings, fetch_spot  # noqa: E402
from notify import push  # noqa: E402

RAW_DIR = ROOT / "data" / "watch_raw"
SNAPSHOT_DIR = ROOT / "data" / "holdings_snapshot"


SESSION_AM = (dtime(9, 31), dtime(11, 29))
SESSION_PM = (dtime(13, 1), dtime(14, 59))


def in_session(now: datetime) -> bool:
    t = now.time()
    if SESSION_AM[0] <= t <= SESSION_AM[1]:
        return True
    if SESSION_PM[0] <= t <= SESSION_PM[1]:
        return True
    return False


def alert_key(code: str, kind: str) -> str:
    return f"{code}::{kind}"


def evaluate(row: dict, watch_map: dict, hold_map: dict) -> list[tuple[str, str]]:
    """对单只票，返回 [(alert_kind, message)] 列表。"""
    code = row["代码"]
    name = row.get("名称") or watch_map.get(code, {}).get("name") or hold_map.get(code, {}).get("name") or ""
    price = row.get("最新价")
    pct = row.get("涨跌幅")
    vol_ratio = row.get("量比")

    if price is None or pct is None:
        return []

    alerts: list[tuple[str, str]] = []
    h = hold_map.get(code)
    w = watch_map.get(code)

    # 持仓优先级最高
    if h:
        sl = h.get("stop_loss")
        if sl and price <= sl:
            alerts.append(("hold_stop", f"💥 持仓跌破止损 · {code} {name} · 现价 {price} ≤ 止损 {sl}（成本 {h.get('cost')}）→ 立即出"))
        if pct is not None and pct <= -5:
            alerts.append(("hold_dump", f"🚨 持仓砸盘 · {code} {name} · 涨跌 {pct}% · 检查止损"))
        if pct is not None and vol_ratio is not None and abs(pct) >= 5 and vol_ratio >= 2:
            alerts.append(("hold_vol", f"💥 持仓异动放量 · {code} {name} · {pct}% · 量比 {vol_ratio}"))

    if w:
        buy = w.get("buy")
        sl = w.get("stop_loss")
        if sl and price <= sl:
            alerts.append(("watch_stop", f"🚨 观察池跌破止损 · {code} {name} [{w.get('genre')}] · 现价 {price} ≤ 止损 {sl} → 假突破已现，未持仓忽略，已持仓立即出"))
        if buy and price >= buy and pct < 9.8:
            alerts.append(("watch_trigger", f"🚀 观察池触发买点 · {code} {name} [{w.get('genre')}] · 现价 {price} ≥ 买点 {buy}（{pct}%）"))
        if pct >= 9.8:
            alerts.append(("watch_zt", f"✅ 观察池封板 · {code} {name} [{w.get('genre')}] · {pct}% 已涨停"))

    return alerts


def snapshot_holdings(now: datetime) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    src = ROOT / "holdings.yaml"
    dst = SNAPSHOT_DIR / f"{now.strftime('%Y%m%d')}.yaml"
    if src.exists() and not dst.exists():
        dst.write_bytes(src.read_bytes())
        print(f"[watch_loop] holdings 快照 → {dst.name}", flush=True)


def append_raw(now: datetime, df) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{now.strftime('%Y%m%d')}.jsonl"
    round_ts = now.isoformat(timespec="seconds")
    with path.open("a") as f:
        for _, row in df.iterrows():
            rec = {"round_ts": round_ts, **{k: row[k] for k in row.index if row[k] is not None}}
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="跑一轮就退")
    p.add_argument("--interval", type=int, default=90, help="轮询秒数")
    p.add_argument("--no-raw", action="store_true", help="不落全量 jsonl（默认落）")
    args = p.parse_args()
    snapshot_holdings(datetime.now())

    watchlist = load_today_watchlist()
    holdings = load_holdings()
    watch_map = {w["code"]: w for w in watchlist}
    hold_map = {h["code"]: h for h in holdings}
    codes = list(set(watch_map) | set(hold_map))

    if not codes:
        print("[watch_loop] 无观察池 + 持仓，退出")
        return

    print(f"[watch_loop] 监控 {len(codes)} 只票：观察池 {len(watch_map)} 持仓 {len(hold_map)}", flush=True)

    sent: set[str] = set()
    round_idx = 0

    while True:
        now = datetime.now()
        if not in_session(now):
            if args.once:
                print("[watch_loop] 非交易时段 + --once，退出")
                return
            if now.time() > dtime(15, 0):
                print("[watch_loop] 已过 15:00，结束监控")
                return
            print(f"[watch_loop] {now.strftime('%H:%M:%S')} 非交易时段，等待…", flush=True)
            time.sleep(args.interval)
            continue

        round_idx += 1
        try:
            df = fetch_spot(codes)
        except Exception as e:
            print(f"[watch_loop] round {round_idx} fetch 失败: {e}", flush=True)
            if args.once:
                return
            time.sleep(args.interval)
            continue

        print(f"[watch_loop] round {round_idx} {now.strftime('%H:%M:%S')} got {len(df)} rows", flush=True)

        if not args.no_raw:
            try:
                append_raw(now, df)
            except Exception as e:
                print(f"[watch_loop] raw 落盘失败: {e}", flush=True)

        for _, row in df.iterrows():
            for kind, msg in evaluate(row.to_dict(), watch_map, hold_map):
                key = alert_key(row["代码"], kind)
                if key in sent:
                    continue
                sent.add(key)
                try:
                    r = push(f"⏱️ [{now.strftime('%H:%M')}] {msg}", source="stock-intraday-watch")
                    print(f"[watch_loop] PUSH {key} msg_id={r['result']['message_id']}", flush=True)
                except Exception as e:
                    print(f"[watch_loop] push 失败 {key}: {e}", flush=True)

        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

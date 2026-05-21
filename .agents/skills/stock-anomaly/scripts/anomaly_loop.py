"""
持仓+观察池异动捕捉 daemon · 独立后台进程，不调 Codex。

差异化定位（与 watch_loop 同一批股票、不同信号源冗余）：
  watch_loop.py    — 自算 minute-by-minute 价格阈值（±5%、破止损、放量 2x 等）
  anomaly_loop.py  — 用 akshare stock_changes_em 标注事件（60日新高、火箭、封板、炸板）
                     60 日新高是 watch_loop 算不出的；akshare 封板/炸板比自算更权威

数据源：akshare stock_changes_em（东财异动池，多 symbol）
监控时段：交易日 09:31-11:29 + 13:01-14:59
轮询频率：90 秒（与 watch_loop 错开 ±10s）

扫描的 anomaly symbol（每轮全部扫一遍）：
  · 火箭发射 — 3 分钟急涨 ≥ 5%  → 整轮聚合成 1 条 digest 推送（开盘可能很多）
  · 封涨停板 — 新封板             → 单只 ping
  · 涨停打开 — 炸板（情绪杀器）   → 单只 ping
  · 60日新高 — 中线突破           → 单只 ping

去重：(code, kind) 一会话只推一次；新一轮只看 时间 > 上轮最大时间 的条目。
过滤：**只推**持仓 + 今日观察池中的代码（不推全市场，避免 TG 洪流）。
      持仓+观察池为空时 daemon 直接退出，节省资源。
      load_today_watchlist 和 load_holdings 在启动时加载一次，
      盘中修改 holdings.yaml 不会被 daemon 看到（需重启）。

用法：
  uv run anomaly_loop.py
  uv run anomaly_loop.py --once
  uv run anomaly_loop.py --interval 60
"""

from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents" / "skills" / "stock-intraday" / "scripts"))

from fetch_realtime import load_today_watchlist, load_holdings  # noqa: E402
from stock_codex.infra.notify import push  # noqa: E402
from stock_codex.infra.logger import get_logger, init_req_id_from_env  # noqa: E402

init_req_id_from_env()
log = get_logger("anomaly_loop")

RAW_DIR = ROOT / "data" / "anomaly_raw"
SNAPSHOT_DIR = ROOT / "data" / "holdings_snapshot"

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


def _parse_rocket_info(info: str) -> tuple[str, str]:
    """akshare 火箭发射 相关信息 = '3分钟涨幅,现价,3分钟涨幅'（第3段冗余）。
    返回 (现价, 涨幅%) 的人类可读字符串。解析失败回退到原始字符串。"""
    try:
        parts = str(info).split(",")
        chg = float(parts[0]) * 100
        price = float(parts[1])
        return f"{price:.2f} 元", f"3 分钟 +{chg:.1f}%"
    except (ValueError, IndexError):
        return info, ""


def format_rocket_digest(now: datetime, entries: list[dict]) -> str:
    """火箭发射本轮聚合 digest。entries 已是仅持仓+观察池命中的 row.to_dict()。"""
    head = f"🚀 [{now.strftime('%H:%M')}] 观察池加速中（派别 D 触发参考）· {len(entries)} 只"
    lines = [head]
    for row in entries[:8]:
        code = row["代码"]
        name = row["名称"]
        price_s, chg_s = _parse_rocket_info(row.get("相关信息", ""))
        if chg_s:
            lines.append(f"- {code} {name}  {price_s}  {chg_s}")
        else:
            lines.append(f"- {code} {name}  {price_s}")
    if len(entries) > 8:
        lines.append(f"- …还有 {len(entries) - 8} 只")
    return "\n".join(lines)


def snapshot_holdings(now: datetime) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    src = ROOT / "holdings.yaml"
    dst = SNAPSHOT_DIR / f"{now.strftime('%Y%m%d')}.yaml"
    if src.exists() and not dst.exists():
        dst.write_bytes(src.read_bytes())
        log.info("holdings 快照 → %s", dst.name)


def append_raw(now: datetime, symbol: str, df) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{now.strftime('%Y%m%d')}.jsonl"
    round_ts = now.isoformat(timespec="seconds")
    with path.open("a") as f:
        for _, row in df.iterrows():
            rec = {
                "round_ts": round_ts,
                "symbol": symbol,
                "code": row["代码"],
                "name": row["名称"],
                "time": fmt_time(row.get("时间")),
                "info": row.get("相关信息", ""),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=90)
    p.add_argument("--no-raw", action="store_true", help="不落全量 jsonl（默认落）")
    args = p.parse_args()

    watched = {w["code"] for w in load_today_watchlist()} | {h["code"] for h in load_holdings()}
    if not watched:
        log.info("持仓+观察池均为空，无监控目标，退出")
        return
    log.info("监控持仓+观察池 %d 只", len(watched))
    snapshot_holdings(datetime.now())

    sent: set[tuple[str, str]] = set()
    last_max_time: dict[str, str] = {sym: "" for sym in SYMBOLS}
    round_idx = 0

    while True:
        now = datetime.now()
        if not in_session(now):
            if args.once:
                log.info("非交易时段 + --once，退出")
                return
            if now.time() > dtime(15, 0):
                log.info("已过 15:00，结束监控")
                return
            log.info("%s 非交易时段，等待…", now.strftime("%H:%M:%S"))
            time.sleep(args.interval)
            continue

        round_idx += 1
        total_new = 0
        rocket_buffer: list[dict] = []  # 本轮命中持仓/观察池的火箭条目，整轮末压成 1 条 digest
        for symbol, label in SYMBOLS.items():
            try:
                df = fetch_anomaly(symbol)
            except Exception:
                log.exception("round %d %s fetch 失败", round_idx, symbol)
                continue
            if df is None or df.empty:
                continue

            if not args.no_raw:
                try:
                    append_raw(now, symbol, df)
                except Exception:
                    log.exception("raw 落盘失败 %s", symbol)

            new_max = last_max_time[symbol]
            for _, row in df.iterrows():
                code = row["代码"]
                t = fmt_time(row.get("时间"))
                if code not in watched:
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
                if symbol == "火箭发射":
                    rocket_buffer.append(row.to_dict())
                else:
                    msg = format_alert(now, symbol, label, row.to_dict())
                    try:
                        r = push(msg, source="stock-anomaly")
                        log.info("PUSH %s %s msg_id=%s", code, symbol, r["result"]["message_id"])
                    except Exception:
                        log.exception("push 失败 %s %s", code, symbol)
                if t > new_max:
                    new_max = t
            last_max_time[symbol] = new_max

        if rocket_buffer:
            digest = format_rocket_digest(now, rocket_buffer)
            try:
                r = push(digest, source="stock-anomaly")
                log.info("PUSH 火箭 digest x%d msg_id=%s",
                         len(rocket_buffer), r["result"]["message_id"])
            except Exception:
                log.exception("push 火箭 digest 失败")

        log.info("round %d %s 新增 %d 条", round_idx, now.strftime("%H:%M:%S"), total_new)

        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.critical("anomaly_loop 顶层崩溃", exc_info=True)
        raise

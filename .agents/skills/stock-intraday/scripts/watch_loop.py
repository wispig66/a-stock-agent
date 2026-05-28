"""
盘中阈值轮询脚本 · 独立后台进程，不调 Codex。

监控对象：今日 decision_tickets / 旧观察池 + holdings.yaml 持仓
监控时段：交易日 09:31-11:29 + 13:01-14:59（避开 4 时点和 Codex 卡片重叠）
轮询频率：90 秒一次
触发条件（每只票同一类型一次会话只推一次）：
  · 💥 跌破止损：current ≤ holdings.stop_loss → 强推
  · 🚨 跌破观察池止损：current ≤ watchlist.stop_loss
  · 🚀 触发主攻/备选买点：watchlist.buy ≤ current ≤ max_chase_price 或 buy * 1.03
  · 🟡 潜伏低吸区：ambush.entry_low ≤ current ≤ ambush.entry_high，只低吸不追高
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
from datetime import date, datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_realtime import load_today_watchlist, load_holdings, fetch_spot  # noqa: E402
from stock_codex.infra.notify import push  # noqa: E402
from stock_codex.infra.logger import get_logger, init_req_id_from_env  # noqa: E402
from stock_codex.infra.db import connect as db_connect  # noqa: E402
from stock_codex.domain import decision as decision_lib  # noqa: E402

init_req_id_from_env()
log = get_logger("watch_loop")

RAW_DIR = ROOT / "data" / "watch_raw"
SNAPSHOT_DIR = ROOT / "data" / "holdings_snapshot"
DB = ROOT / "data" / "daily.db"


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


def evaluate(
    row: dict,
    watch_map: dict,
    hold_map: dict,
    today: date | None = None,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """对单只票，返回 [(alert_kind, message)] 列表。

    持仓告警在锁仓期（today < unlock_date）改文案为"🌙 锁仓中 · 明早处理"，
    并把 kind 加 `_locked` 后缀以与解锁版去重隔离。
    """
    code = row["代码"]
    name = row.get("名称") or watch_map.get(code, {}).get("name") or hold_map.get(code, {}).get("name") or ""
    price = row.get("最新价")
    pct = row.get("涨跌幅")
    vol_ratio = row.get("量比")

    if price is None or pct is None:
        return []

    if today is None:
        today = date.today()
    if now is None:
        now = datetime.now()

    alerts: list[tuple[str, str]] = []
    h = hold_map.get(code)
    w = watch_map.get(code)

    # 持仓优先级最高
    if h:
        locked = _is_locked(h, today)
        sl = h.get("stop_loss")
        if sl and price <= sl:
            if locked:
                alerts.append((
                    "hold_stop_locked",
                    f"🌙 锁仓中 · 持仓跌破止损 · {code} {name} · 现价 {price} ≤ 止损 {sl}（成本 {h.get('cost')}）· 不可出，明早集合竞价处理",
                ))
            else:
                alerts.append((
                    "hold_stop",
                    f"💥 持仓跌破止损 · {code} {name} · 现价 {price} ≤ 止损 {sl}（成本 {h.get('cost')}）→ 立即出",
                ))
        if pct is not None and pct <= -5:
            if locked:
                alerts.append((
                    "hold_dump_locked",
                    f"🌙 锁仓中 · 持仓砸盘 · {code} {name} · 涨跌 {pct}% · T+1 不可出，明早决策",
                ))
            else:
                alerts.append((
                    "hold_dump",
                    f"🚨 持仓砸盘 · {code} {name} · 涨跌 {pct}% · 检查止损",
                ))
        if pct is not None and vol_ratio is not None and abs(pct) >= 5 and vol_ratio >= 2:
            if locked:
                alerts.append((
                    "hold_vol_locked",
                    f"🌙 锁仓中 · 持仓异动放量 · {code} {name} · {pct}% · 量比 {vol_ratio}（仅信息）",
                ))
            else:
                alerts.append((
                    "hold_vol",
                    f"💥 持仓异动放量 · {code} {name} · {pct}% · 量比 {vol_ratio}",
                ))

    if w:
        lane = w.get("lane")
        buy = w.get("buy")
        sl = w.get("stop_loss")
        entry_low = w.get("entry_low")
        entry_high = w.get("entry_high")
        if sl and price <= sl:
            alerts.append(("watch_stop", f"🚨 观察池跌破止损 · {code} {name} [{w.get('genre')}] · 现价 {price} ≤ 止损 {sl} → 假突破已现，未持仓忽略，已持仓立即出"))
        if lane == "ambush":
            if entry_low and entry_high and entry_low <= price <= entry_high:
                alerts.append((
                    "ambush_zone",
                    _actionable_message(code, name, w, price, pct, "低吸区"),
                ))
        elif buy and price >= buy and price <= (w.get("max_chase_price") or buy * 1.03) and pct < 9.8:
            if lane == "backup" and not _backup_can_trigger(watch_map, now):
                alerts.append((
                    "backup_wait",
                    f"👀 仅观察 · {code} {name} [{w.get('genre')}] · 已到备选买点 {buy}，但主攻未过截止时间，暂不下单",
                ))
            else:
                reason = "趋势买点" if lane == "trend" else "触发买点"
                alerts.append(("watch_trigger", _actionable_message(code, name, w, price, pct, reason)))
        if pct >= 9.8:
            if lane == "backup" and not _backup_can_trigger(watch_map, now):
                alerts.append(("watch_zt_observe", f"👀 仅观察封板 · {code} {name} [{w.get('genre')}] · {pct}% 已涨停；主攻未过截止时间，不追备选"))
            else:
                alerts.append(("watch_zt", f"✅ 观察池封板 · {code} {name} [{w.get('genre')}] · {pct}% 已涨停"))

    return alerts


def _actionable_message(code: str, name: str, w: dict, price: float, pct: float, reason: str) -> str:
    entry_low = w.get("entry_low")
    entry_high = w.get("entry_high")
    max_chase = w.get("max_chase_price")
    stop = w.get("stop_loss")
    deadline = w.get("deadline_time")
    size = w.get("position_max_pct")
    zone = f"{entry_low}-{entry_high}" if entry_low is not None and entry_high is not None else str(w.get("buy"))
    chase = f"；最多追价 {max_chase}" if max_chase is not None else ""
    return (
        f"✅ 可下单信号 · {code} {name} [{w.get('genre')}] · {reason} · "
        f"现价 {price}（{pct}%）· 触发价/区间 {zone}{chase} · "
        f"仓位 ≤{size}% · 止损 {stop} · 截止 {deadline}"
    )


def _backup_can_trigger(watch_map: dict, now: datetime) -> bool:
    mains = [w for w in watch_map.values() if w.get("lane") == "main"]
    if not mains:
        return True
    if any(w.get("status") in {"triggered", "bought"} for w in mains):
        return False
    return all(_deadline_passed(w.get("deadline_time"), now) for w in mains)


def _deadline_passed(deadline: str | None, now: datetime) -> bool:
    if not deadline:
        return False
    try:
        if len(deadline) <= 5 and ":" in deadline:
            parts = [int(p) for p in deadline.split(":")]
            hour, minute = parts[0], parts[1]
            return now.time() >= dtime(hour, minute)
        return date.fromisoformat(deadline) <= now.date()
    except (TypeError, ValueError):
        return False


def _is_locked(h: dict, today: date) -> bool:
    """根据 hold_map 条目和当前日期判定是否锁仓。

    unlock_date 缺失或解析失败一律视为已解锁（保护老条目）。
    """
    ud = h.get("unlock_date")
    if not ud:
        return False
    try:
        return date.fromisoformat(ud) > today
    except (TypeError, ValueError):
        return False


def _mark_dynamic_status(trade_date: str, code: str, status: str) -> bool:
    with db_connect(DB) as conn:
        cur = conn.execute(
            """UPDATE watchlist_dynamic
               SET status=?
               WHERE trade_date=? AND code=?""",
            (status, trade_date, code),
        )
        return cur.rowcount > 0


def snapshot_holdings(now: datetime) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    src = ROOT / "holdings.yaml"
    dst = SNAPSHOT_DIR / f"{now.strftime('%Y%m%d')}.yaml"
    if src.exists() and not dst.exists():
        dst.write_bytes(src.read_bytes())
        log.info("holdings 快照 → %s", dst.name)


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
        log.info("无观察池 + 持仓，退出")
        return

    log.info("监控 %d 只票：观察池 %d 持仓 %d", len(codes), len(watch_map), len(hold_map))

    sent: set[str] = set()
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
        try:
            df = fetch_spot(codes)
        except Exception:
            log.exception("round %d fetch 失败", round_idx)
            if args.once:
                return
            time.sleep(args.interval)
            continue

        log.info("round %d %s got %d rows", round_idx, now.strftime("%H:%M:%S"), len(df))

        if not args.no_raw:
            try:
                append_raw(now, df)
            except Exception:
                log.exception("raw 落盘失败 round=%d", round_idx)

        for _, row in df.iterrows():
            for kind, msg in evaluate(row.to_dict(), watch_map, hold_map):
                key = alert_key(row["代码"], kind)
                if key in sent:
                    continue
                pushed = False
                try:
                    push_result = push(f"⏱️ [{now.strftime('%H:%M')}] {msg}", source="stock-intraday-watch")
                    pushed = True
                    sent.add(key)
                    msg_id = (push_result.get("result") or {}).get("message_id") if isinstance(push_result, dict) else None
                    log.info("PUSH %s msg_id=%s", key, msg_id)
                except Exception:
                    log.exception("push 失败 %s", key)
                if kind in {"watch_trigger", "ambush_zone"}:
                    w = watch_map.get(row["代码"]) or {}
                    lane = w.get("lane")
                    if lane and pushed:
                        try:
                            if w.get("source") == "watchlist_dynamic":
                                updated = _mark_dynamic_status(now.strftime("%Y-%m-%d"), row["代码"], "triggered")
                            else:
                                updated = decision_lib.mark_ticket_status(
                                    DB, now.strftime("%Y-%m-%d"), row["代码"], lane, "triggered"
                                )
                            if updated:
                                w["status"] = "triggered"
                        except Exception:
                            log.exception("观察池状态回写失败 %s %s", row["代码"], lane)

        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.critical("watch_loop 顶层崩溃", exc_info=True)
        raise

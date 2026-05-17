"""周复盘数据聚合 + 长文渲染 + machine-readable YAML 读写。

主入口：
  build_weekly_data_pack(end_date) -> dict       本地数据聚合
  render_long_form(pack, web_pack, parts) -> str 长文渲染
  parse_machine_readable(path) -> dict | None    L1 消费用解析
"""
from __future__ import annotations
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "daily.db"


def _db_path() -> Path:
    override = os.environ.get("STOCK_DB_PATH")
    return Path(override) if override else DEFAULT_DB


def _last_friday(end_date: date) -> date:
    """end_date 当周的周五（周日传入 → 上周五）。"""
    offset = (end_date.weekday() - 4) % 7
    return end_date - timedelta(days=offset)


def _week_label(monday: date) -> str:
    iso_year, iso_week, _ = monday.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def build_weekly_data_pack(end_date: date) -> dict:
    """聚合本周（周一 ~ 周五）本地数据。

    end_date 通常是周日 21:00 触发时的当前日期。
    返回 dict 结构见 spec §5。
    """
    friday = _last_friday(end_date)
    monday = friday - timedelta(days=4)
    week_days = [monday + timedelta(days=i) for i in range(5)]
    week_days_str = [d.isoformat() for d in week_days]

    pack: dict = {
        "week_label": _week_label(monday),
        "monday": monday.isoformat(),
        "friday": friday.isoformat(),
        "trading_days_in_week": 0,
        "sentiment_series": [],
        "top_gainers": [],
        "limit_up_ladder": [],
        "lhb_seats": [],
        "ths_hot_reasons": [],
        "weekly_trades": [],
        "anomaly_files": [],
    }

    db = _db_path()
    if not db.exists():
        return pack

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(week_days_str))
        # sentiment
        rows = conn.execute(
            f"SELECT * FROM sentiment_daily WHERE date IN ({placeholders}) ORDER BY date",
            week_days_str,
        ).fetchall()
        pack["sentiment_series"] = [dict(r) for r in rows]
        pack["trading_days_in_week"] = len(rows)

        # top gainers (按周累计涨幅 Top 20)
        rows = conn.execute(
            f"""
            SELECT code,
                   MAX(CASE WHEN date=? THEN close END) AS friday_close,
                   MIN(CASE WHEN date=? THEN open  END) AS monday_open,
                   SUM(amount) AS week_amount
            FROM daily_kline
            WHERE date IN ({placeholders})
            GROUP BY code
            HAVING friday_close IS NOT NULL AND monday_open IS NOT NULL AND monday_open > 0
            ORDER BY (friday_close - monday_open) / monday_open DESC
            LIMIT 20
            """,
            [week_days_str[-1], week_days_str[0], *week_days_str],
        ).fetchall()
        pack["top_gainers"] = [
            {
                "code": r["code"],
                "week_pct": (r["friday_close"] - r["monday_open"]) / r["monday_open"] * 100,
                "amount": r["week_amount"],
            }
            for r in rows
        ]

        # 涨停梯队（每日 max_consec from sentiment_daily 已有）
        pack["limit_up_ladder"] = [
            {"date": s["date"], "max_consec": s["max_consec"], "lu": s["limit_up_count"]}
            for s in pack["sentiment_series"]
        ]

        # 龙虎榜（席位频次）
        rows = conn.execute(
            f"""
            SELECT seat_name, COUNT(*) AS n, SUM(net_amount) AS net_sum
            FROM lhb WHERE date IN ({placeholders}) AND seat_name IS NOT NULL
            GROUP BY seat_name ORDER BY n DESC, net_sum DESC LIMIT 15
            """,
            week_days_str,
        ).fetchall()
        pack["lhb_seats"] = [dict(r) for r in rows]

        # ths_hot_reason 题材热度
        rows = conn.execute(
            f"""
            SELECT date, code, name, reason FROM ths_hot_reason
            WHERE date IN ({placeholders}) ORDER BY date DESC LIMIT 200
            """,
            week_days_str,
        ).fetchall()
        pack["ths_hot_reasons"] = [dict(r) for r in rows]

        # 个人交易
        rows = conn.execute(
            "SELECT ts, code, side, price, qty, reason, note "
            "FROM trades WHERE date(ts) BETWEEN ? AND ? ORDER BY ts",
            [week_days_str[0], week_days_str[-1]],
        ).fetchall()
        pack["weekly_trades"] = [dict(r) for r in rows]
    finally:
        conn.close()

    # 异动日报路径（存在性 check）
    anomaly_dir = ROOT / "data" / "anomaly_findings"
    for d in week_days_str:
        compact = d.replace("-", "")
        p = anomaly_dir / f"{compact}.md"
        if p.exists():
            pack["anomaly_files"].append(str(p))

    return pack

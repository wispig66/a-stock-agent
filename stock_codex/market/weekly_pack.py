"""周复盘数据聚合 + 长文渲染 + machine-readable YAML 读写。

主入口：
  build_weekly_data_pack(end_date) -> dict       本地数据聚合
  render_long_form(pack, parts) -> str             长文渲染
  parse_machine_readable(path) -> dict | None    L1 消费用解析
"""
from __future__ import annotations
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    import yaml  # PyYAML
except ImportError as e:
    raise ImportError("weekly_pack 依赖 PyYAML：uv add pyyaml") from e
from stock_codex.paths import DATA_DIR, DB_FILE as DEFAULT_DB


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
    anomaly_dir = DATA_DIR / "anomaly_findings"
    for d in week_days_str:
        compact = d.replace("-", "")
        p = anomaly_dir / f"{compact}.md"
        if p.exists():
            pack["anomaly_files"].append(str(p))

    return pack


_YAML_FENCE_RE = re.compile(
    r"## 下周方向 \(machine-readable\)\s*\n+```ya?ml\s*\n(.*?)\n```",
    re.DOTALL,
)


def render_long_form(pack: dict, parts: dict) -> str:
    """渲染 data/weekly_review/YYYY-WW.md 长文。

    parts 由 skill 阶段（Codex）合成：
      part1_narrative, part2_narrative, themes, discipline_notes, web_status
    """
    week_num = pack["week_label"].split("-W")[1]
    generated_at = datetime.now(timezone(timedelta(hours=8))).isoformat(
        timespec="seconds"
    )

    machine = {
        "week": pack["week_label"],
        "generated_at": generated_at,
        "sentiment_stage": _infer_stage(pack),
        "themes": parts["themes"],
        "discipline_notes": parts.get("discipline_notes", ""),
        "web_status": parts.get("web_status", "ok"),
    }
    yaml_block = yaml.safe_dump(
        machine, allow_unicode=True, sort_keys=False, default_flow_style=False
    )

    return (
        f"# W{week_num} 周复盘 ({pack['monday']} ~ {pack['friday']})\n\n"
        f"_交易日：{pack['trading_days_in_week']}/5_\n\n"
        f"## Part 1 本周复盘\n\n{parts['part1_narrative']}\n\n"
        f"## Part 2 下周方向\n\n{parts['part2_narrative']}\n\n"
        f"## 下周方向 (machine-readable)\n\n"
        f"```yaml\n{yaml_block}```\n\n"
        f"## 数据附录\n\n"
        f"{_render_appendix(pack)}\n"
    )


def _infer_stage(pack: dict) -> str:
    if not pack["sentiment_series"]:
        return "数据缺失"
    return pack["sentiment_series"][-1].get("phase") or "未知"


def _render_appendix(pack: dict) -> str:
    lines = ["### 板块/个股周涨幅 Top"]
    for g in pack["top_gainers"][:10]:
        lines.append(f"- {g['code']}: {g['week_pct']:+.2f}%")
    lines.append("\n### 连板梯队")
    for d in pack["limit_up_ladder"]:
        lines.append(f"- {d['date']}: 涨停 {d['lu']} 家, 最高连板 {d['max_consec']}")
    if pack["weekly_trades"]:
        lines.append("\n### 个人交易明细")
        for t in pack["weekly_trades"]:
            lines.append(
                f"- {t['ts']} {t['side']} {t['code']} @ {t['price']} × {t['qty']} ({t['reason'] or '-'})"
            )
    return "\n".join(lines)


def parse_machine_readable(path: Path) -> Optional[dict]:
    """从落地长文中解析 machine-readable YAML 块。失败/缺失返回 None。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = _YAML_FENCE_RE.search(text)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None

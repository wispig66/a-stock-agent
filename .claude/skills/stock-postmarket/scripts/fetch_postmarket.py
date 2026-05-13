"""
盘后数据归集 · 写库 + 渲染盘后 fact pack。

每个交易日 15:30 后跑：
1. 拉今日完整收盘数据（涨停/炸板/龙虎榜）
2. UPSERT 写 sentiment_daily（让明日 L1 阶段判定有 10 日基线）
3. UPSERT 写 ths_hot_reason（同花顺人工题材标签 + 个股清单）
4. 渲染 postmarket fact pack md（与 premarket 同一份字典）

LLM 后续读这份 fact pack + 今天的 Telegram 推送做复盘 / 明日预案。
"""

from __future__ import annotations
import argparse
import sqlite3
import sys
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd
import akshare as ak

# 共用 premarket 的 extras（reason tags / 全市场龙虎榜）
PROJECT_ROOT = Path(__file__).resolve().parents[4]
PREMARKET_SCRIPTS = PROJECT_ROOT / ".claude/skills/stock-premarket/scripts"
sys.path.insert(0, str(PREMARKET_SCRIPTS))
try:
    from extras import ths_hot_reason, daily_dragon_tiger
    _EXTRAS_OK = True
except Exception as _e:
    print(f"[warn] extras 加载失败 {_e}", file=sys.stderr)
    _EXTRAS_OK = False

warnings.filterwarnings("ignore")

DB = PROJECT_ROOT / "data" / "daily.db"
OUT_DIR = PROJECT_ROOT / "data" / "fact_pack"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT_ROOT / "code"))
from db import connect as db_connect  # noqa: E402


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ============================================================
# 数据拉取
# ============================================================

def fetch_zt(date: str) -> pd.DataFrame:
    """涨停池（清洗异常涨跌幅）"""
    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is None or df.empty:
            return pd.DataFrame()
        return df[df["涨跌幅"] >= 9.9].copy()
    except Exception as e:
        log(f"涨停池失败: {e}")
        return pd.DataFrame()


def fetch_zd(date: str) -> pd.DataFrame:
    """跌停池"""
    try:
        df = ak.stock_zt_pool_dtgc_em(date=date)
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        log(f"跌停池失败: {e}")
        return pd.DataFrame()


def fetch_zb(date: str) -> pd.DataFrame:
    """炸板池"""
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date)
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        log(f"炸板池失败: {e}")
        return pd.DataFrame()


def fetch_qs(date: str) -> pd.DataFrame:
    """强势股池（昨日涨停 → 今日继续强势 / 晋级 2 板的候选）"""
    try:
        df = ak.stock_zt_pool_strong_em(date=date)
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        log(f"强势股池失败: {e}")
        return pd.DataFrame()


# ============================================================
# 情绪指标计算（与 SKILL.md 阈值规则对齐）
# ============================================================

def compute_sentiment(date: str, zt: pd.DataFrame, zd: pd.DataFrame,
                      zb: pd.DataFrame, qs_yesterday: pd.DataFrame) -> dict:
    """计算 sentiment_daily 一行记录。

    promotion_rate（晋级率）= 今日 ≥2 板 / 昨日 1 板涨停（无昨日数据时填 NULL）
    blast_rate（炸板率）= 炸板数 / (炸板数 + 涨停数)
    phase 用 fetch_data.py SKILL 同套阈值。
    """
    n_zt = len(zt)
    n_zd = len(zd)
    n_zb = len(zb)
    max_consec = int(zt["连板数"].max()) if not zt.empty and "连板数" in zt.columns else 0
    n_2plus = len(zt[zt["连板数"] >= 2]) if not zt.empty and "连板数" in zt.columns else 0

    blast_rate = round(n_zb / (n_zb + n_zt) * 100, 2) if (n_zb + n_zt) > 0 else None

    # 晋级率 = 今日 ≥2 板 / 昨日强势股池里的 1 板数（粗略口径）
    promotion_rate = None
    if not qs_yesterday.empty and n_zt > 0:
        try:
            promotion_rate = round(n_2plus / max(1, len(qs_yesterday)) * 100, 2)
        except Exception:
            promotion_rate = None

    # phase 判定（与 SKILL.md 3a 表对齐）
    if n_zt < 30:
        phase = "退潮"
    elif n_zt < 60 and (blast_rate or 0) >= 30:
        phase = "高位分歧"
    elif n_zt < 60:
        phase = "启动"
    elif max_consec >= 5:
        phase = "加速"
    else:
        phase = "加速候选"

    # 赚钱效应（粗略）：昨日涨停今日继续涨 - 昨日涨停今日下跌
    # 暂用强势股池家数作 proxy
    money_effect = len(qs_yesterday) if not qs_yesterday.empty else None
    loss_effect = n_zd  # 简化：跌停家数当亏钱效应 proxy

    return {
        "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}",
        "limit_up_count": n_zt,
        "limit_down_count": n_zd,
        "max_consec": max_consec,
        "promotion_rate": promotion_rate,
        "second_promotion_rate": None,
        "blast_rate": blast_rate,
        "money_effect": money_effect,
        "loss_effect": loss_effect,
        "phase": phase,
    }


# ============================================================
# 写库 UPSERT
# ============================================================

def upsert_sentiment(sent: dict):
    with db_connect(DB) as conn:
        conn.execute("""
            INSERT INTO sentiment_daily
                (date, limit_up_count, limit_down_count, max_consec,
                 promotion_rate, second_promotion_rate, blast_rate,
                 money_effect, loss_effect, phase)
            VALUES
                (:date, :limit_up_count, :limit_down_count, :max_consec,
                 :promotion_rate, :second_promotion_rate, :blast_rate,
                 :money_effect, :loss_effect, :phase)
            ON CONFLICT(date) DO UPDATE SET
                limit_up_count=excluded.limit_up_count,
                limit_down_count=excluded.limit_down_count,
                max_consec=excluded.max_consec,
                promotion_rate=excluded.promotion_rate,
                blast_rate=excluded.blast_rate,
                money_effect=excluded.money_effect,
                loss_effect=excluded.loss_effect,
                phase=excluded.phase
        """, sent)
        conn.commit()
    log(f"sentiment_daily UPSERT {sent['date']} phase={sent['phase']}")


def upsert_ths_hot(date_iso: str, hot: pd.DataFrame):
    if hot.empty:
        log("ths_hot 空，跳过写库")
        return
    rows = []
    for _, r in hot.iterrows():
        rows.append({
            "date": date_iso,
            "code": str(r.get("代码", "")),
            "name": str(r.get("名称", "")),
            "close": _float(r.get("收盘价")),
            "change_pct": _float(r.get("涨幅%")),
            "turnover_pct": _float(r.get("换手率%")),
            "amount": _float(r.get("成交额")),
            "big_net": _float(r.get("大单净量")),
            "reason": str(r.get("题材归因", "")),
        })
    with db_connect(DB) as conn:
        conn.executemany("""
            INSERT INTO ths_hot_reason
                (date, code, name, close, change_pct, turnover_pct,
                 amount, big_net, reason)
            VALUES
                (:date, :code, :name, :close, :change_pct, :turnover_pct,
                 :amount, :big_net, :reason)
            ON CONFLICT(date, code) DO UPDATE SET
                close=excluded.close,
                change_pct=excluded.change_pct,
                turnover_pct=excluded.turnover_pct,
                amount=excluded.amount,
                big_net=excluded.big_net,
                reason=excluded.reason
        """, rows)
        conn.commit()
    log(f"ths_hot_reason UPSERT {date_iso} 共 {len(rows)} 条")


def _float(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


# ============================================================
# 渲染盘后 fact pack
# ============================================================

def render_pack(date: str, sent: dict, zt: pd.DataFrame, zd: pd.DataFrame,
                zb: pd.DataFrame, hot: pd.DataFrame, lhb: dict) -> str:
    iso = sent["date"]
    lines = [f"# 盘后 fact pack · {iso}", ""]

    lines.append("## 一、今日情绪快照")
    lines.append("")
    lines.append(f"- 涨停：**{sent['limit_up_count']}** · 跌停：**{sent['limit_down_count']}** · 最高连板：**{sent['max_consec']}** 板")
    lines.append(f"- 炸板率：{sent['blast_rate']}% · 晋级率：{sent['promotion_rate'] or 'N/A'}%")
    lines.append(f"- **阶段判定：{sent['phase']}**")
    lines.append("")

    lines.append("## 二、连板梯队")
    lines.append("")
    if zt.empty:
        lines.append("- 无数据")
    else:
        dist = "  ".join(f"{n}板 {c}只" for n, c
                         in zt["连板数"].value_counts().sort_index().items())
        lines.append(f"- 连板分布：{dist}")
        top = zt.sort_values("连板数", ascending=False).head(10)
        for _, r in top.iterrows():
            seal = r.get("封板资金", 0) / 1e8
            price = r.get("最新价", 0)
            lines.append(f"  - {r['代码']} {r['名称']} [{r.get('所属行业','-')}] {int(r['连板数'])}板  ¥{price:.2f}  封单 {seal:.2f}亿")
    lines.append("")

    lines.append("## 三、跌停 / 炸板")
    lines.append("")
    lines.append(f"- 跌停 {len(zd)} 只 · 炸板 {len(zb)} 只")
    if not zb.empty and "炸板次数" in zb.columns:
        worst = zb.nlargest(5, "炸板次数")
        lines.append("- 炸板最猛 Top 5：")
        for _, r in worst.iterrows():
            lines.append(f"  - {r.get('代码','?')} {r.get('名称','?')} 炸板 {r.get('炸板次数','?')} 次")
    lines.append("")

    lines.append("## 四、龙虎榜净买入 Top 8（含上榜原因）")
    lines.append("")
    stocks = lhb.get("stocks", []) if isinstance(lhb, dict) else []
    if not stocks:
        lines.append("- 无数据")
    else:
        for s in stocks[:8]:
            lines.append(f"- {s['code']} {s['name']}  净买 **{s['net_buy_wan']/10000:.2f}亿**  · {s.get('reason','')}")
    lines.append("")

    lines.append("## 五、同花顺今日题材标签 Top 10（reason 拆分）")
    lines.append("")
    if hot.empty or "题材归因" not in hot.columns:
        lines.append("- 无数据")
    else:
        tally: dict[str, list[str]] = {}
        for _, r in hot.iterrows():
            reason = str(r.get("题材归因", "") or "").strip()
            tag_str = f"{r.get('代码', '')} {r.get('名称', '')}"
            for t in (x.strip() for x in reason.split("+") if x.strip()):
                tally.setdefault(t, []).append(tag_str)
        ranked = sorted(tally.items(), key=lambda x: -len(x[1]))[:10]
        lines.append("| 题材 | 出现次数 | 代表个股 |")
        lines.append("|------|---------|---------|")
        for k, v in ranked:
            lines.append(f"| {k} | {len(v)} | {' / '.join(v[:3])} |")
    lines.append("")

    lines.append("## 六、近 10 日情绪表（含今日）")
    lines.append("")
    with db_connect(DB) as conn:
        recent = pd.read_sql(
            "SELECT date, limit_up_count, limit_down_count, max_consec,"
            " promotion_rate, blast_rate, phase"
            " FROM sentiment_daily ORDER BY date DESC LIMIT 10", conn)
    if recent.empty:
        lines.append("- 仅今日数据，明日开始可见趋势")
    else:
        recent = recent.sort_values("date")
        lines.append("| 日期 | 涨停 | 跌停 | 高度 | 晋级率 | 炸板率 | 阶段 |")
        lines.append("|------|------|------|------|--------|--------|------|")
        for _, r in recent.iterrows():
            lines.append(f"| {r['date']} | {r['limit_up_count']} | {r['limit_down_count']} "
                         f"| {r['max_consec']} | {r['promotion_rate'] or '-'} "
                         f"| {r['blast_rate'] or '-'} | {r['phase']} |")
    lines.append("")
    lines.append("---")
    lines.append(f"_生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}_")
    return "\n".join(lines)


# ============================================================
# 入口
# ============================================================

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYYMMDD，默认今天（盘后用今天，盘前回测用历史日）")
    p.add_argument("--dry", action="store_true", help="只渲染不写库")
    args = p.parse_args()

    date = args.date or datetime.now().strftime("%Y%m%d")
    iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    log(f"盘后归集 {iso}")

    zt = fetch_zt(date)
    zd = fetch_zd(date)
    zb = fetch_zb(date)
    qs = fetch_qs(date)  # 注意：strong_em 是今日强势股，做晋级率用昨日数据更准，这里先粗略
    log(f"涨停 {len(zt)} 跌停 {len(zd)} 炸板 {len(zb)} 强势池 {len(qs)}")

    sent = compute_sentiment(date, zt, zd, zb, qs)
    log(f"phase={sent['phase']} max_consec={sent['max_consec']}")

    # 同花顺热点（盘后 15:30+ 才有当日数据）
    hot = pd.DataFrame()
    if _EXTRAS_OK:
        try:
            hot = ths_hot_reason(date=iso)
            log(f"同花顺热点 {len(hot)} 条")
        except Exception as e:
            log(f"同花顺热点失败: {e}")

    # 全市场龙虎榜
    lhb = {"stocks": []}
    if _EXTRAS_OK:
        try:
            lhb = daily_dragon_tiger(trade_date=iso)
            log(f"龙虎榜 {lhb.get('total_records', 0)} 条")
        except Exception as e:
            log(f"龙虎榜失败: {e}")

    if not args.dry:
        upsert_sentiment(sent)
        upsert_ths_hot(iso, hot)

    pack = render_pack(date, sent, zt, zd, zb, hot, lhb)
    out = OUT_DIR / f"{date}_postmarket.md"
    out.write_text(pack, encoding="utf-8")
    log(f"已写 {out}")
    print(pack)


if __name__ == "__main__":
    main()

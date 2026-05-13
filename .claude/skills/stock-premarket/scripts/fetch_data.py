"""
盘前 fact pack 生成器（核心版）。

默认行为：拉最近一个交易日数据，渲染核心 fact pack，写入 data/fact_pack/YYYYMMDD_premarket.md 并打到 stdout。

LLM 想看某题材详情时：
    python fetch_data.py --concept "电网设备"

数据源选择遵循 project_stock_data_sources 项目记忆：
- 涨停池 stock_zt_pool_em（清洗 涨跌幅 >= 9.9）
- 炸板池 stock_zt_pool_zbgc_em
- 龙虎榜 stock_lhb_detail_em（如失败，记录无数据）
- 概念板块 stock_board_concept_cons_ths（同花顺，东财不通）
- 历史情绪 SQLite sentiment_daily
"""

from __future__ import annotations
import argparse
import sqlite3
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import akshare as ak

# 扩展数据源（同目录 extras.py，抽自 a-stock-data 仓库）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from extras import ths_hot_reason, daily_dragon_tiger
    _EXTRAS_OK = True
except Exception as _e:
    print(f"[warn] extras 加载失败 {_e}，将回退到 akshare 旧源", file=sys.stderr)
    _EXTRAS_OK = False

warnings.filterwarnings("ignore")
pd.set_option("display.max_columns", None)

ROOT = Path(__file__).resolve().parents[4]  # stock-premarket/scripts -> skills -> .claude -> stock
DB = ROOT / "data" / "daily.db"

sys.path.insert(0, str(ROOT / "code"))
from db import connect as db_connect  # noqa: E402
OUT_DIR = ROOT / "data" / "fact_pack"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def last_trade_day() -> str:
    """返回最近一个已完整收盘的交易日。

    规则：
    - 当前时间 < 15:00 → 跳过今天（盘前/盘中数据不完整），从昨天倒推
    - 当前时间 ≥ 15:00 → 从今天倒推
    - 倒推时跳过没有有效涨停池的日子（节假日/周末）
    - 至少 5 只涨停才视为有效交易日
    """
    now = datetime.now()
    start_delta = 0 if now.hour >= 15 else 1
    for delta in range(start_delta, start_delta + 10):
        d = (now - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=d)
            if df is not None and len(df) >= 5:
                log(f"  使用最近完整收盘日 {d}（涨停 {len(df)} 只）")
                return d
        except Exception as e:
            log(f"  尝试 {d} 失败: {type(e).__name__}")
            continue
    return (now - timedelta(days=1)).strftime("%Y%m%d")


# ============ 数据拉取（每个独立 try-except，单点失败不影响整体） ============

def fetch_zt_pool(date: str) -> pd.DataFrame:
    df = ak.stock_zt_pool_em(date=date)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[df["涨跌幅"] >= 9.9].copy()  # 清洗异常涨跌幅
    return df


def fetch_zb_pool(date: str) -> pd.DataFrame:
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date)
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        log(f"炸板池失败: {e}")
        return pd.DataFrame()


def fetch_lhb(date: str) -> pd.DataFrame:
    """龙虎榜。优先用 extras.daily_dragon_tiger（东财 datacenter，更稳，
    含上榜原因 + 净买额排名），失败回退 akshare stock_lhb_detail_em。
    返回统一 schema：代码 / 名称 / 净买入 / 上榜原因（如有）。
    """
    iso_date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

    if _EXTRAS_OK:
        try:
            r = daily_dragon_tiger(trade_date=iso_date)
            stocks = r.get("stocks", [])
            if stocks:
                df = pd.DataFrame(stocks)
                df = df.rename(columns={
                    "code": "代码", "name": "名称",
                    "net_buy_wan": "净买额_万",
                    "reason": "上榜原因",
                })
                df["净买入"] = df["净买额_万"] * 10000  # 元
                log(f"龙虎榜 {iso_date}：daily_dragon_tiger 返回 {len(df)} 只")
                return df
            else:
                log(f"龙虎榜 {iso_date}：daily_dragon_tiger 空，回退 akshare")
        except Exception as e:
            log(f"daily_dragon_tiger 失败 {type(e).__name__}: {str(e)[:80]}，回退 akshare")

    try:
        df = ak.stock_lhb_detail_em(start_date=date, end_date=date)
        if df is None or df.empty:
            log(f"龙虎榜 {date}：akshare 也空")
            return pd.DataFrame()
        return df
    except Exception as e:
        log(f"龙虎榜 {date} akshare 失败: {type(e).__name__}: {str(e)[:80]}")
        return pd.DataFrame()


def fetch_ths_hot(date: str) -> pd.DataFrame:
    """同花顺热点强势股 + 题材归因 reason tags。
    date: YYYYMMDD（fetch_data 内部约定），转 YYYY-MM-DD 调用。
    返回 DataFrame，含「代码/名称/题材归因/涨幅%/换手率%/成交额/大单净量」。
    盘前调拿的是 D-1 盘后数据。
    """
    if not _EXTRAS_OK:
        return pd.DataFrame()
    iso_date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    try:
        df = ths_hot_reason(date=iso_date)
        if df is None or df.empty:
            log(f"同花顺热点 {iso_date}：空")
            return pd.DataFrame()
        log(f"同花顺热点 {iso_date}：{len(df)} 只")
        return df
    except Exception as e:
        log(f"同花顺热点 {iso_date} 失败: {type(e).__name__}: {str(e)[:100]}")
        return pd.DataFrame()


def historical_sentiment(days: int = 10) -> pd.DataFrame:
    if not DB.exists():
        return pd.DataFrame()
    with db_connect(DB) as conn:
        try:
            df = pd.read_sql(
                f"SELECT * FROM sentiment_daily ORDER BY date DESC LIMIT {days}", conn
            )
            return df.sort_values("date") if not df.empty else df
        except Exception as e:
            log(f"历史情绪失败: {e}")
            return pd.DataFrame()


# ============ 聚合 ============

def hot_industries(zt: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """从涨停股聚合行业（粗）。TODO 升级为同花顺概念维度。"""
    if zt.empty or "所属行业" not in zt.columns:
        return pd.DataFrame()
    out_rows = []
    for ind, grp in zt.groupby("所属行业"):
        top = grp.sort_values("连板数", ascending=False).iloc[0]
        top_price = top.get('最新价', 0)
        out_rows.append({
            "行业": ind,
            "涨停数": len(grp),
            "最高连板": int(grp["连板数"].max()),
            "龙头": f"{top['代码']} {top['名称']} (封板价 ¥{top_price:.2f})",
        })
    return pd.DataFrame(out_rows).sort_values("涨停数", ascending=False).head(top_n)


def lhb_highlights(lhb: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """龙虎榜净买入榜。兼容两种来源：
    - extras.daily_dragon_tiger（含「上榜原因」列）
    - akshare stock_lhb_detail_em（无上榜原因，需要嗅探列名）
    """
    if lhb.empty:
        return pd.DataFrame()
    code_col = next((c for c in ["代码", "股票代码"] if c in lhb.columns), None)
    name_col = next((c for c in ["名称", "股票名称"] if c in lhb.columns), None)
    net_col = next((c for c in ["净买入", "净买额", "净买入额", "龙虎榜净买额"]
                    if c in lhb.columns), None)
    if not all([code_col, name_col, net_col]):
        log(f"龙虎榜列名不识别: {list(lhb.columns)[:10]}")
        return pd.DataFrame()

    keep = [code_col, name_col, net_col]
    if "上榜原因" in lhb.columns:
        keep.append("上榜原因")
    df = lhb[keep].copy()
    df = df.rename(columns={code_col: "代码", name_col: "名称", net_col: "净买入"})
    # 同一只票去重（akshare 多席位时多行）
    if "上榜原因" in df.columns:
        df = df.groupby(["代码", "名称", "上榜原因"], as_index=False)["净买入"].sum()
    else:
        df = df.groupby(["代码", "名称"], as_index=False)["净买入"].sum()
    return df.nlargest(top_n, "净买入")


def reason_tag_agg(hot: pd.DataFrame, top_n: int = 8) -> pd.DataFrame:
    """同花顺 reason 标签拆分聚合：把「人形机器人+PCB+算力」拆开按出现次数排序。
    返回 [题材, 出现次数, 代表个股(前3只)] 三列。
    """
    if hot.empty or "题材归因" not in hot.columns:
        return pd.DataFrame()
    tally: dict[str, list[str]] = {}
    for _, r in hot.iterrows():
        reason = str(r.get("题材归因", "") or "").strip()
        if not reason:
            continue
        tag_str = f"{r.get('代码', '')} {r.get('名称', '')}".strip()
        for tag in (t.strip() for t in reason.split("+") if t.strip()):
            tally.setdefault(tag, []).append(tag_str)
    if not tally:
        return pd.DataFrame()
    rows = [{"题材": k, "出现次数": len(v),
             "代表个股": " / ".join(v[:3])}
            for k, v in tally.items()]
    return pd.DataFrame(rows).sort_values("出现次数", ascending=False).head(top_n)


# ============ 渲染 ============

def render_core_pack(date: str) -> str:
    lines = [f"# 盘前 fact pack · {date}", ""]

    zt = fetch_zt_pool(date)
    lines.append("## 一、涨停结构")
    lines.append("")
    if zt.empty:
        lines.append("- 无数据")
    else:
        dist = "  ".join(f"{n}板 {c}只" for n, c in zt["连板数"].value_counts().sort_index().items())
        lines.append(f"- 涨停总数：**{len(zt)} 只**（清洗后）")
        lines.append(f"- 连板分布：{dist}")
        top_consec = zt[zt["连板数"] == zt["连板数"].max()]
        lines.append(f"- 最高连板：{int(zt['连板数'].max())} 板")
        for _, r in top_consec.iterrows():
            seal = r.get('封板资金', 0) / 1e8
            turn = r.get('换手率', 0)
            turn_s = f"{turn:.2f}" if isinstance(turn, (int, float)) else str(turn)
            price = r.get('最新价', 0)
            lines.append(f"  - {r['代码']} {r['名称']} [{r.get('所属行业','-')}] 封板价 **¥{price:.2f}** 封单 {seal:.2f} 亿 换手 {turn_s}%")

        # 新增：所有连板（2板+）个股详情清单，供 LLM 派别 A 计算买点
        multi = zt[zt["连板数"] >= 2].sort_values("连板数", ascending=False)
        if not multi.empty:
            lines.append("")
            lines.append(f"- **连板个股清单**（≥2 板，共 {len(multi)} 只，含封板价用于派别 A 买点 = 封板价 × 1.01）：")
            for _, r in multi.iterrows():
                seal = r.get('封板资金', 0) / 1e8
                price = r.get('最新价', 0)
                lines.append(f"  - {r['代码']} {r['名称']} [{r.get('所属行业','-')}] {int(r['连板数'])}板 封板价 ¥{price:.2f} 封单 {seal:.2f}亿")

    lines.append("## 二、热门行业 Top 5（按涨停股数）")
    lines.append("")
    hi = hot_industries(zt)
    if hi.empty:
        lines.append("- 无数据")
    else:
        lines.append("| 行业 | 涨停数 | 最高连板 | 龙头 |")
        lines.append("|------|--------|----------|------|")
        for _, r in hi.iterrows():
            lines.append(f"| {r['行业']} | {r['涨停数']} | {r['最高连板']}板 | {r['龙头']} |")
    lines.append("")

    lines.append("## 三、炸板池")
    lines.append("")
    zb = fetch_zb_pool(date)
    if zb.empty:
        lines.append("- 无数据")
    else:
        lines.append(f"- 炸板总数：{len(zb)} 只")
        # 显示炸板次数最多的 Top 5（炸板次数高 = 抛压重 = 退潮信号）
        if "炸板次数" in zb.columns:
            top_blast = zb.nlargest(5, "炸板次数")
        else:
            top_blast = zb.head(5)
        lines.append("- 炸板最猛 Top 5：")
        for _, r in top_blast.iterrows():
            pct = r.get('涨跌幅', 0)
            pct_s = f"{pct:.2f}" if isinstance(pct, (int, float)) else str(pct)
            lines.append(f"  - {r.get('代码','?')} {r.get('名称','?')} 涨跌幅 {pct_s}% 炸板 {r.get('炸板次数','?')} 次")
    lines.append("")

    lines.append("## 四、龙虎榜要点（净买入 Top 5）")
    lines.append("")
    lhb = fetch_lhb(date)
    if lhb.empty:
        lines.append("- 无数据（接口失败或当日无龙虎榜）")
    else:
        hi_lhb = lhb_highlights(lhb)
        if hi_lhb.empty:
            lines.append(f"- 共 {len(lhb)} 条记录，但字段不识别，跳过聚合")
        else:
            for _, r in hi_lhb.iterrows():
                reason_part = f"  · {r['上榜原因']}" if "上榜原因" in r and r['上榜原因'] else ""
                lines.append(f"- {r['代码']} {r['名称']}  净买入 **{r['净买入']/1e8:.2f} 亿**{reason_part}")
    lines.append("")

    # ========== 五、同花顺热点 reason tags ==========
    lines.append("## 五、同花顺热点 · 题材归因（D-1 盘后数据）")
    lines.append("")
    hot = fetch_ths_hot(date)
    if hot.empty:
        lines.append("- 无数据（盘后 15:30+ 才有当日数据；或网络失败）")
    else:
        lines.append(f"_共 {len(hot)} 只强势股，逐只带同花顺编辑部人工标注的题材标签_")
        lines.append("")
        # 5.1 题材标签出现频次 Top 8（机器可读维度）
        tags = reason_tag_agg(hot, top_n=8)
        if not tags.empty:
            lines.append("### 5.1 题材标签出现频次 Top 8（reason 拆分后聚合）")
            lines.append("")
            lines.append("| 题材 | 出现次数 | 代表个股（最多 3 只）|")
            lines.append("|------|---------|---------------------|")
            for _, r in tags.iterrows():
                lines.append(f"| {r['题材']} | {r['出现次数']} | {r['代表个股']} |")
            lines.append("")
        # 5.2 个股清单（前 30 只，含完整 reason）
        lines.append("### 5.2 强势股清单（前 30 只，含完整 reason）")
        lines.append("")
        show_cols = [c for c in ["代码", "名称", "题材归因", "涨幅%", "换手率%"]
                     if c in hot.columns]
        for _, r in hot.head(30).iterrows():
            parts = [str(r.get(c, "")) for c in show_cols]
            lines.append(f"- {parts[0]} {parts[1]}：**{r.get('题材归因','')}**"
                         + (f"  涨幅 {r.get('涨幅%','?')}% 换手 {r.get('换手率%','?')}%"
                            if "涨幅%" in hot.columns else ""))
    lines.append("")

    lines.append("## 六、近 10 日情绪指标")
    lines.append("")
    sent = historical_sentiment(10)
    if sent.empty:
        lines.append("- 数据积累中。sentiment_daily 表为空，需要先跑盘后模块写入历史数据。")
    else:
        lines.append("| 日期 | 涨停 | 跌停 | 高度 | 晋级率 | 炸板率 | 阶段 |")
        lines.append("|------|------|------|------|--------|--------|------|")
        for _, r in sent.iterrows():
            lines.append(f"| {r['date']} | {r['limit_up_count']} | {r['limit_down_count']} | {r['max_consec']} | {r.get('promotion_rate','-')} | {r.get('blast_rate','-')} | {r.get('phase','-')} |")
    lines.append("")

    lines.append("---")
    lines.append(f"_生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}_")
    return "\n".join(lines)


def render_concept_detail(concept_name: str) -> str:
    """LLM 想看单一题材成分股时调。"""
    try:
        df = ak.stock_board_concept_cons_ths(symbol=concept_name)
    except Exception as e:
        return f"# 题材详情 · {concept_name}\n\n获取失败: {e}"
    out = [f"# 题材详情 · {concept_name}", "", f"成分股 {len(df)} 只", ""]
    cols = [c for c in ["代码", "名称", "现价", "涨跌幅", "成交额", "流通市值"] if c in df.columns]
    if cols:
        out.append(df[cols].head(30).to_markdown(index=False))
    else:
        out.append(df.head(30).to_markdown(index=False))
    return "\n".join(out)


# ============ 入口 ============

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="YYYYMMDD，默认最近交易日")
    parser.add_argument("--concept", default=None, help="单一题材详情模式")
    args = parser.parse_args()

    if args.concept:
        print(render_concept_detail(args.concept))
        return

    date = args.date or last_trade_day()
    log(f"使用日期 {date}")
    pack = render_core_pack(date)
    out_path = OUT_DIR / f"{date}_premarket.md"
    out_path.write_text(pack, encoding="utf-8")
    log(f"已写入 {out_path}")
    print(pack)


if __name__ == "__main__":
    main()

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
# 高度梯队断层判定（5/18 教训：梯队断层是次日加速失败的领先指标）
# ============================================================

def compute_ladder_gap(zt: pd.DataFrame) -> dict:
    """判断连板梯队是否断层。

    断层定义：≥2 板梯队中存在跨级（如 6→4 跳过 5），最大跨级 ≥ 2。

    返回 {ladder_gap, max_gap_size, missing_steps, top_consec_dist}。
    """
    if zt is None or zt.empty or "连板数" not in zt.columns:
        return {"ladder_gap": False, "max_gap_size": 0, "missing_steps": [],
                "top_consec_dist": {}}
    dist = zt["连板数"].value_counts().to_dict()
    levels = sorted(int(k) for k in dist if int(k) >= 2)
    if len(levels) < 2:
        return {"ladder_gap": False, "max_gap_size": 0, "missing_steps": [],
                "top_consec_dist": {str(k): int(v) for k, v in dist.items()}}
    max_gap = 0
    missing: list[int] = []
    for a, b in zip(levels, levels[1:]):
        gap = b - a
        if gap > max_gap:
            max_gap = gap
        if gap > 1:
            missing.extend(range(a + 1, b))
    return {
        "ladder_gap": max_gap >= 2,
        "max_gap_size": int(max_gap),
        "missing_steps": missing,
        "top_consec_dist": {str(k): int(v) for k, v in dist.items()},
    }


# ============================================================
# 隔夜外围（盘后写卡时最关心：美股收盘 + 期指夜盘 + 大宗/汇率）
# ============================================================

def fetch_global_markets() -> dict:
    """从新浪 hq.sinajs.cn 抓隔夜外围一组数据。

    源稳定不走代理；任一失败返回空 {}（卡片只写"未抓到"，不阻塞 L4）。

    返回字段（pct 已转 % 数值）：
      us_dji / us_ixic / us_spx        # 三大指数（昨夜收盘）
      futures_nq / futures_es           # 纳指/标普期指（夜盘实时）
      cmd_oil / cmd_gold                # WTI 原油 / 黄金期货
      fx_usdcnh                         # 美元/离岸人民币
    """
    import requests
    syms = "gb_$dji,gb_$ixic,gb_$inx,hf_NQ,hf_ES,hf_CL,hf_GC,fx_susdcnh"
    try:
        r = requests.get(
            f"https://hq.sinajs.cn/list={syms}",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=6,
        )
        r.encoding = "gbk"
        text = r.text
    except Exception as e:
        log(f"global_markets 抓取失败: {e}")
        return {}

    # 新浪不同前缀字段位不同，分别解析
    def _parse_line(line: str) -> tuple[str, list[str]] | None:
        if "=\"" not in line:
            return None
        head, _, rest = line.partition("=\"")
        sym = head.rsplit("_", 1)[-1].lstrip("$").lower()
        fields = rest.strip("\";").split(",")
        return sym, fields

    out: dict[str, dict] = {}
    label_map = {
        "dji": ("us_dji", "DJI 道指"),
        "ixic": ("us_ixic", "纳指"),
        "inx": ("us_spx", "标普 500"),
        "nq": ("futures_nq", "纳指期指"),
        "es": ("futures_es", "标普期指"),
        "cl": ("cmd_oil", "WTI 原油"),
        "gc": ("cmd_gold", "黄金期货"),
        "susdcnh": ("fx_usdcnh", "美元/离岸人民币"),
    }
    for line in text.splitlines():
        parsed = _parse_line(line)
        if not parsed:
            continue
        sym, fields = parsed
        if sym not in label_map:
            continue
        key, label = label_map[sym]
        # gb_/hf_/fx_ 字段排布差异较大；用兜底：试取 price + change_pct
        price = chg_pct = None
        # gb_ : name, price, change, change_pct, ...
        # hf_ : price, ..., pct?  ← 不稳定，按常见位置兜底试
        # 优先策略：找两个 float-able 字段
        floats = []
        for f in fields:
            try:
                floats.append(float(f))
            except (TypeError, ValueError):
                continue
        if not floats:
            continue
        if sym in ("dji", "ixic", "inx"):
            # gb_ schema: [name, price, change_abs, change_pct, ...]
            try:
                price = float(fields[1]); chg_pct = float(fields[2])
            except (IndexError, ValueError):
                price = floats[0]
        elif sym in ("nq", "es", "cl", "gc"):
            # hf_ schema 大致：[bid, ask, ..., last, ..., open, high, low, prev_close]
            # 用 last + prev_close 算 pct
            try:
                last = float(fields[3]); prev = float(fields[7]) if len(fields) > 7 else 0
                price = last
                if prev > 0:
                    chg_pct = round((last - prev) / prev * 100, 2)
            except (IndexError, ValueError):
                price = floats[0]
        elif sym == "susdcnh":
            # fx_ schema: [time, bid, ask, ..., last]
            try:
                price = float(fields[1])
            except (IndexError, ValueError):
                price = floats[0]
        out[key] = {"label": label, "price": price, "chg_pct": chg_pct}
    return out


# ============================================================
# 渲染盘后 fact pack
# ============================================================

def render_pack(date: str, sent: dict, zt: pd.DataFrame, zd: pd.DataFrame,
                zb: pd.DataFrame, hot: pd.DataFrame, lhb: dict,
                ladder: dict | None = None,
                global_markets: dict | None = None) -> str:
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
        if ladder and ladder.get("ladder_gap"):
            miss = ladder.get("missing_steps") or []
            miss_str = "/".join(str(x) + "板" for x in miss) or "—"
            lines.append(f"- ⚠️ **梯队断层**：跨级 {ladder.get('max_gap_size', 0)} 板 · 缺位 {miss_str}")
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
    lines.append("## 七、隔夜外围")
    lines.append("")
    gm = global_markets or {}
    if not gm:
        lines.append("- 抓取失败或暂无数据")
    else:
        order = ["us_dji", "us_ixic", "us_spx", "futures_nq", "futures_es",
                 "cmd_oil", "cmd_gold", "fx_usdcnh"]
        for k in order:
            v = gm.get(k)
            if not v:
                continue
            price = v.get("price")
            chg = v.get("chg_pct")
            chg_str = f" ({chg:+.2f}%)" if chg is not None else ""
            price_str = f"{price:.2f}" if price is not None else "?"
            lines.append(f"- {v.get('label', k)}：{price_str}{chg_str}")
    lines.append("")
    lines.append("---")
    lines.append(f"_生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}_")
    return "\n".join(lines)


# ============================================================
# ALLOWED 段（卡片校验唯一事实源；见 docs/allowed_schema.md）
# ============================================================

def build_allowed(*, iso: str, sent: dict, zt: pd.DataFrame, zd: pd.DataFrame,
                  zb: pd.DataFrame, hot: pd.DataFrame, lhb: dict,
                  ladder: dict | None = None,
                  global_markets: dict | None = None) -> dict:
    codes: dict[str, str] = {}
    lianban: dict[str, int] = {}
    pct: dict[str, float] = {}
    concepts: list[str] = []

    def _add(df: pd.DataFrame, take_lianban: bool = False):
        if df is None or df.empty:
            return
        for _, r in df.iterrows():
            code = str(r.get("代码") or "")
            name = str(r.get("名称") or "")
            if not code or len(code) != 6:
                continue
            codes[code] = name or codes.get(code, name)
            try:
                pct[code] = round(float(r.get("涨跌幅")), 2)
            except (TypeError, ValueError):
                pass
            if take_lianban:
                try:
                    lianban[code] = int(r.get("连板数"))
                except (TypeError, ValueError):
                    pass

    _add(zt, take_lianban=True)
    _add(zd)
    _add(zb)

    if hot is not None and not hot.empty:
        for _, r in hot.iterrows():
            code = str(r.get("代码") or "")
            name = str(r.get("名称") or "")
            if code and len(code) == 6:
                codes[code] = name or codes.get(code, name)
            reason = str(r.get("题材归因", "") or "").strip()
            for t in (x.strip() for x in reason.split("+") if x.strip()):
                if t not in concepts:
                    concepts.append(t)

    for s in (lhb or {}).get("stocks") or []:
        code = str(s.get("code") or "")
        name = str(s.get("name") or "")
        if code and len(code) == 6:
            codes[code] = name or codes.get(code, name)

    ladder = ladder or {}
    summary = {
        "date": iso,
        "limit_up": int(sent.get("limit_up_count") or 0),
        "limit_down": int(sent.get("limit_down_count") or 0),
        "broken": int(len(zb)) if zb is not None and not zb.empty else 0,
        "max_consec": int(sent.get("max_consec") or 0),
        "phase": str(sent.get("phase") or ""),
        "ladder_gap": bool(ladder.get("ladder_gap", False)),
        "ladder_max_gap_size": int(ladder.get("max_gap_size", 0)),
        "ladder_missing_steps": list(ladder.get("missing_steps", [])),
    }

    return {
        "schema_version": "1",
        "skill": "stock-postmarket",
        "snapshot_at": datetime.now().replace(microsecond=0).isoformat(),
        "codes": codes,
        "lianban": lianban,
        "pct": pct,
        "summary": summary,
        "concepts": concepts[:30],
        "news": [],
        "global_markets": global_markets or {},
    }


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

    # 梯队断层 + 隔夜外围（5/18 加入）
    ladder = compute_ladder_gap(zt)
    log(f"ladder_gap={ladder['ladder_gap']} max_gap={ladder['max_gap_size']}")
    global_markets = fetch_global_markets()
    log(f"global_markets {len(global_markets)} 项")

    if not args.dry:
        upsert_sentiment(sent)
        upsert_ths_hot(iso, hot)

    pack = render_pack(date, sent, zt, zd, zb, hot, lhb,
                       ladder=ladder, global_markets=global_markets)
    out = OUT_DIR / f"{date}_postmarket.md"
    out.write_text(pack, encoding="utf-8")
    log(f"已写 {out}")
    print(pack)

    # ALLOWED 段：卡片校验事实源
    import json as _json
    allowed = build_allowed(iso=iso, sent=sent, zt=zt, zd=zd, zb=zb, hot=hot, lhb=lhb,
                            ladder=ladder, global_markets=global_markets)
    print("\n=== ALLOWED ===")
    print(_json.dumps(allowed, ensure_ascii=False, indent=2))
    print("=== /ALLOWED ===")
    allowed_file = PROJECT_ROOT / "data" / "allowed_latest_stock-postmarket.json"
    allowed_file.parent.mkdir(parents=True, exist_ok=True)
    allowed_file.write_text(_json.dumps(allowed, ensure_ascii=False, indent=2))
    log(f"已写 {allowed_file}")


if __name__ == "__main__":
    main()

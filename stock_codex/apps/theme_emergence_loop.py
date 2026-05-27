"""
盘中新主线浮现识别 daemon · Layer 1。

每 60 秒一个 tick：
  ① 拉 ak.stock_changes_em（4 类异动）+ ak.stock_zt_pool_em（涨停池）
  ② 映射到一级题材（concept_whitelist.yaml）
  ③ 每题材一个 PageHinkley detector，喂异动事件计数
  ④ 检查 5 个信号 → T1 / T2 触发 → 写 theme_emergence_log
  ⑤ T2 触发：选 2-3 只候选写 watchlist_dynamic + 推 TG

监控时段：09:30-11:30 / 13:00-15:00
学习期：09:30-10:00（喂数据但不触发告警，避开集合竞价噪声）

数据落盘：
  data/anomaly_raw/{date}.jsonl  全市场异动原始事件（与 anomaly_loop 兼容文件名）
  data/daily.db                  4 张新表（见 init_db.sql 末尾）

用法：
  uv run theme_emergence_loop.py
  uv run theme_emergence_loop.py --once
  uv run theme_emergence_loop.py --interval 60 --push-level=t2
"""

from __future__ import annotations
import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta

import yaml

from stock_codex.infra.db import connect_close as db_connect  # noqa: E402 — daemon 用自动关闭版
from stock_codex.infra.notify import push  # noqa: E402
from stock_codex.infra.logger import get_logger, init_req_id_from_env  # noqa: E402
from stock_codex.paths import DATA_DIR, DB_FILE

init_req_id_from_env()
log = get_logger("theme_emergence_loop")

DB = DB_FILE
WHITELIST = DATA_DIR / "concept_whitelist.yaml"
RAW_DIR = DATA_DIR / "anomaly_raw"

SESSION_AM = (dtime(9, 30), dtime(11, 30))
SESSION_PM = (dtime(13, 0), dtime(15, 0))
LEARNING_END = dtime(10, 0)
DEFAULT_INTERVAL = 60

# PH 参数（首版起点，calibration 后调）
# 喂入是每 tick 该题材的事件计数（0/1/2/3...），lambda 表示累积值偏离 baseline 的阈值
# 5 ≈ "短期内 ~5-10 个事件超出基线"。T1 只审计，T2 才推送，优先降低发现延迟
PH_LAMBDA = 5.0
PH_DELTA = 0.05
PH_MIN_SAMPLES = 20

# 涨停簇集口径
CLUSTER_WINDOW_MIN = 30
CLUSTER_THRESHOLD = 3

# T2 反身性保护
T2_PERSISTENCE_MIN = 10   # T1 持续 10min 才能升 T2，避免确认过晚
T2_MIN_SIGNALS = 2        # 至少 2 个独立信号

# 异动 symbol 订阅（与 anomaly_loop 一致）
ANOMALY_SYMBOLS = ["火箭发射", "封涨停板", "打开涨停板", "60日新高"]

# 推送级别：shadow 只审计；t2 只推主线确认；all 推 T1+T2。
PUSH_LEVEL = "t2"

# PH 状态保存节流（每 N tick 才存一次，避免每 60s 一次的高频写库）
SAVE_PH_EVERY_N_TICKS = 5


def _norm_seal_time(t) -> str:
    """'092500' -> '09:25:00'；datetime.time -> 'HH:MM:SS'；空 -> ''"""
    if t is None or t == "":
        return ""
    if hasattr(t, "strftime"):
        return t.strftime("%H:%M:%S")
    t = str(t)
    if len(t) == 6 and t.isdigit():
        return f"{t[0:2]}:{t[2:4]}:{t[4:6]}"
    return t


# ─────────────────────────────────────────────────────
# Page-Hinkley detector（轻量自实现，避免 river 依赖膨胀）
# ─────────────────────────────────────────────────────
class PageHinkley:
    """单边漂移检测（向上突变敏感）。
    update(x) → drift_detected: bool
    state: (m_t, min_m_t, n)
    """
    def __init__(self, lamb=PH_LAMBDA, delta=PH_DELTA, min_samples=PH_MIN_SAMPLES):
        self.lamb = lamb
        self.delta = delta
        self.min_samples = min_samples
        self.reset()

    def reset(self):
        self.x_mean = 0.0
        self.m_t = 0.0
        self.min_m_t = 0.0
        self.n = 0
        self.drift_detected = False

    def update(self, x: float) -> bool:
        self.n += 1
        self.x_mean += (x - self.x_mean) / self.n
        self.m_t += x - self.x_mean - self.delta
        self.min_m_t = min(self.min_m_t, self.m_t)
        ph = self.m_t - self.min_m_t
        if self.n >= self.min_samples and ph > self.lamb:
            self.drift_detected = True
        else:
            self.drift_detected = False
        return self.drift_detected

    def snapshot(self) -> dict:
        return {"m_t": self.m_t, "min_m_t": self.min_m_t, "n": self.n,
                "x_mean": self.x_mean}

    def restore(self, d: dict):
        self.m_t = d.get("m_t", 0.0)
        self.min_m_t = d.get("min_m_t", 0.0)
        self.n = d.get("n", 0)
        self.x_mean = d.get("x_mean", 0.0)


# ─────────────────────────────────────────────────────
# Concept whitelist
# ─────────────────────────────────────────────────────
class Whitelist:
    """题材白名单 + 反向索引。

    匹配优先级：member（白名单 hardcoded）→ keyword 在 (name + concept_cache[code]) 上 → None
    concept_cache 从 ths_hot_reason 表加载近 30 日数据，code → reason 文本串。
    """
    def __init__(self, themes: dict, code_idx: dict[str, str],
                 kw_idx: list[tuple[str, str]],
                 concept_cache: dict[str, str]):
        self.themes = themes
        self.code_idx = code_idx
        self.kw_idx = kw_idx
        self.concept_cache = concept_cache

    def __len__(self):
        return len(self.themes)


def load_whitelist() -> Whitelist:
    if not WHITELIST.exists():
        log.warning("concept_whitelist.yaml 不存在，所有事件归入 fallback")
        return Whitelist({}, {}, [], {})
    with WHITELIST.open() as f:
        themes = yaml.safe_load(f) or {}
    code_idx, kw_idx = {}, []
    for tag, conf in themes.items():
        for c in (conf or {}).get("members") or []:
            code_idx[str(c).zfill(6)] = tag
        for kw in (conf or {}).get("keywords") or []:
            kw_idx.append((kw, tag))
    # 从 ths_hot_reason 拉近 30 日 code → reason，作为 keyword 匹配的语料。
    # 这只是增强匹配的可选缓存；新环境、测试库或未初始化 DB 可以没有这张表。
    concept_cache: dict[str, str] = {}
    if not DB.exists():
        log.warning("daily.db 不存在，concept_cache 为空: %s", DB)
    else:
        try:
            with db_connect(DB) as conn:
                has_table = conn.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='ths_hot_reason'"""
                ).fetchone()
                if has_table:
                    cur = conn.execute(
                        """SELECT code, GROUP_CONCAT(reason, ' ') FROM ths_hot_reason
                           WHERE date >= date('now', '-30 day')
                           GROUP BY code""")
                    for code, reason in cur.fetchall():
                        concept_cache[str(code).zfill(6)] = reason or ""
                else:
                    log.warning("ths_hot_reason 表不存在，concept_cache 为空")
        except Exception:
            log.exception("加载 ths_hot_reason 失败，concept_cache 为空")
    log.info("白名单 %d 题材 / %d 成员 / %d 关键词 / cache %d 只股票",
             len(themes), len(code_idx), len(kw_idx), len(concept_cache))
    return Whitelist(themes, code_idx, kw_idx, concept_cache)


def map_to_concept(code: str, name: str, info: str, wl: Whitelist) -> str | None:
    """优先级：member → keyword(name + cache reason + info) → None"""
    code = str(code).zfill(6)
    if code in wl.code_idx:
        return wl.code_idx[code]
    cache_text = wl.concept_cache.get(code, "")
    text = f"{name} {cache_text} {info}"
    for kw, tag in wl.kw_idx:
        if kw in text:
            return tag
    return None


# ─────────────────────────────────────────────────────
# Data fetch
# ─────────────────────────────────────────────────────
def in_session(now: datetime) -> bool:
    t = now.time()
    return (SESSION_AM[0] <= t <= SESSION_AM[1]
            or SESSION_PM[0] <= t <= SESSION_PM[1])


def fetch_anomaly_all(now: datetime):
    """拉全市场异动事件，返回 [(symbol, code, name, time, info)]"""
    import akshare as ak
    rows = []
    for sym in ANOMALY_SYMBOLS:
        for attempt in range(3):
            try:
                df = ak.stock_changes_em(symbol=sym)
                for _, r in df.iterrows():
                    rows.append((sym, str(r["代码"]), r["名称"],
                                 str(r.get("时间", "")), str(r.get("相关信息", ""))))
                break
            except Exception as e:
                if attempt == 2:
                    log.warning("stock_changes_em(%s) 3 次失败: %s", sym, e)
                time.sleep(1)
    return rows


def fetch_zt_pool(today: str):
    """拉今日涨停池，返回 list[dict]"""
    import akshare as ak
    today_compact = today.replace("-", "")
    for attempt in range(3):
        try:
            df = ak.stock_zt_pool_em(date=today_compact)
            return df.to_dict("records") if df is not None and len(df) else []
        except Exception as e:
            if attempt == 2:
                log.warning("stock_zt_pool_em 3 次失败: %s", e)
            time.sleep(1)
    return []


def append_raw(now: datetime, rows: list):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{now.strftime('%Y%m%d')}.jsonl"
    round_ts = now.isoformat(timespec="seconds")
    with path.open("a") as f:
        for sym, code, name, t, info in rows:
            f.write(json.dumps({
                "round_ts": round_ts, "symbol": sym, "code": code,
                "name": name, "time": t, "info": info
            }, ensure_ascii=False) + "\n")


def snapshot_zt_pool(now: datetime, today: str, pool: list, wl: Whitelist):
    if not pool:
        return
    ts = now.isoformat(timespec="seconds")
    with db_connect(DB) as conn:
        for r in pool:
            code = str(r.get("代码", "")).zfill(6)
            # 用所属行业 + 涨停统计作为额外 keyword 语料
            extra = f"{r.get('所属行业', '')} {r.get('涨停统计', '')}"
            concept = map_to_concept(code, r.get("名称", ""), extra, wl)
            conn.execute(
                """INSERT OR REPLACE INTO intraday_limit_up_snapshot
                   (snapshot_ts, trade_date, code, name, limit_up_count,
                    first_seal_time, open_count, seal_amount, concept_top1)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, today, code, r.get("名称"),
                 int(r.get("连板数", 1) or 1),
                 _norm_seal_time(r.get("首次封板时间", "")),
                 int(r.get("炸板次数", 0) or 0),
                 float(r.get("封板资金", 0) or 0),
                 concept)
            )


# ─────────────────────────────────────────────────────
# Signal checks
# ─────────────────────────────────────────────────────
def latest_zt_snapshot(today: str, concept: str) -> list[dict]:
    with db_connect(DB) as conn:
        cur = conn.execute(
            """SELECT code, name, limit_up_count, first_seal_time, open_count, seal_amount
               FROM intraday_limit_up_snapshot
               WHERE trade_date=? AND concept_top1=?
                 AND snapshot_ts = (SELECT MAX(snapshot_ts)
                                    FROM intraday_limit_up_snapshot
                                    WHERE trade_date=?)
            """,
            (today, concept, today))
        return [dict(zip(["code", "name", "limit_up_count", "first_seal_time",
                          "open_count", "seal_amount"], row))
                for row in cur.fetchall()]


def cluster_count_30min(today: str, concept: str, now: datetime) -> int:
    """30 分钟滑窗内**首次封板时间**在该窗口内的同题材股票数。

    口径：first_seal_time 在 [now-30min, now] 内（按 HH:MM:SS 字符串比较）。
    要求 _norm_seal_time 已归一化（092500 → 09:25:00）。
    """
    cutoff_t = (now - timedelta(minutes=CLUSTER_WINDOW_MIN)).strftime("%H:%M:%S")
    now_t = now.strftime("%H:%M:%S")
    with db_connect(DB) as conn:
        cur = conn.execute(
            """SELECT COUNT(DISTINCT code) FROM intraday_limit_up_snapshot
               WHERE trade_date=? AND concept_top1=?
                 AND first_seal_time != ''
                 AND first_seal_time >= ? AND first_seal_time <= ?""",
            (today, concept, cutoff_t, now_t))
        return cur.fetchone()[0]


def check_signals(today: str, concept: str, now: datetime, ph: PageHinkley) -> dict:
    members = latest_zt_snapshot(today, concept)
    cluster = cluster_count_30min(today, concept, now)
    first_seal = None
    first_leader = None
    second_board = False
    for m in members:
        # defensive：若旧数据未归一化（"092500"）这里再 norm 一次
        m["first_seal_time"] = _norm_seal_time(m.get("first_seal_time", ""))
        if m["first_seal_time"]:
            if first_seal is None or m["first_seal_time"] < first_seal:
                first_seal = m["first_seal_time"]
                first_leader = m
        if (m["limit_up_count"] or 0) >= 2:
            second_board = True
    first_seal_ok = (first_seal is not None and first_seal <= "10:30:00"
                     and first_leader and (first_leader.get("open_count") or 0) == 0)
    return {
        "PH": ph.drift_detected,
        "cluster3": cluster >= CLUSTER_THRESHOLD,
        "first_seal_1030": first_seal_ok,
        "second_board": second_board,
        "members": members,
        "first_leader": first_leader,
        "cluster_count": cluster,
        "first_seal_time": first_seal,
    }


# ─────────────────────────────────────────────────────
# Logging + push
# ─────────────────────────────────────────────────────
def already_logged(today: str, concept: str, level: str) -> bool:
    with db_connect(DB) as conn:
        cur = conn.execute(
            "SELECT 1 FROM theme_emergence_log WHERE trade_date=? AND concept_tag=? AND signal_level=?",
            (today, concept, level))
        return cur.fetchone() is not None


def t1_triggered_at(today: str, concept: str) -> datetime | None:
    with db_connect(DB) as conn:
        cur = conn.execute(
            "SELECT detected_at FROM theme_emergence_log WHERE trade_date=? AND concept_tag=? AND signal_level='T1'",
            (today, concept))
        row = cur.fetchone()
        return datetime.fromisoformat(row[0]) if row else None


def log_emergence(today: str, concept: str, level: str, signals: dict,
                  ph: PageHinkley, now: datetime) -> int:
    boolean_signals = {k: v for k, v in signals.items() if isinstance(v, bool)}
    with db_connect(DB) as conn:
        cur = conn.execute(
            """INSERT INTO theme_emergence_log
               (detected_at, trade_date, concept_tag, signal_level, signals_hit,
                cluster_count, first_leader, first_seal_time, ph_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now.isoformat(timespec="seconds"), today, concept, level,
             json.dumps(boolean_signals, ensure_ascii=False),
             signals["cluster_count"],
             (signals["first_leader"] or {}).get("code"),
             signals["first_seal_time"],
             ph.m_t - ph.min_m_t))
        conn.commit()
        return cur.lastrowid


def push_t1_card(concept: str, signals: dict, now: datetime):
    prefix = "[SHADOW] " if PUSH_LEVEL == "shadow" else ""
    lines = [f"{prefix}🟡 主线浮现 · 候选 · {now.strftime('%H:%M')}", "━━━━━━━━━━━━━━",
             f"🏷️ 题材：{concept}",
             f"📊 信号：PH 漂移 + 同板块 30min 内 ≥{signals['cluster_count']} 只涨停"]
    for m in (signals.get("members") or [])[:5]:
        lines.append(f"   - {m['name']} {m['code']} "
                     f"{m['first_seal_time'] or '盘中'}{'封板' if (m['open_count'] or 0) == 0 else f' (炸板{m['open_count']}次)'}")
    lines += ["", f"⏳ T2 确认条件：再持续 {T2_PERSISTENCE_MIN}min 或追加信号",
              "👀 已列入观察，未生成买点（避免追高）"]
    text = "\n".join(lines)
    if not _should_push("T1"):
        log.info("[%s push T1 %s] %s", PUSH_LEVEL.upper(), concept, text[:100])
        return
    push(text, source="theme-loop")


def push_t2_card(concept: str, signals: dict, candidates: list[dict], now: datetime):
    sig = signals
    prefix = "[SHADOW] " if PUSH_LEVEL == "shadow" else ""
    lines = [f"{prefix}🚨 主线确认 · {concept} · {now.strftime('%H:%M')}",
             "━━━━━━━━━━━━━━", "📊 信号命中:",
             f"   {'✅' if sig['PH'] else '⚪'} PH 漂移检测",
             f"   {'✅' if sig['cluster3'] else '⚪'} 30min 涨停 ≥3（实测 {sig['cluster_count']} 只）",
             f"   {'✅' if sig['first_seal_1030'] else '⚪'} 首封龙头 ≤10:30 不炸板"
             + (f"（{sig['first_leader']['name']} {sig['first_seal_time']}）" if sig.get('first_leader') else ""),
             f"   {'✅' if sig['second_board'] else '⚪'} 板块内已现 2 连板"]
    if candidates:
        lines += ["", "🎯 候选（已写入动态观察池，等待后续可下单条件）："]
        for i, c in enumerate(candidates, 1):
            lines.append(f"{i}. {c['name']} {c['code']} [{c['discipline_type']}派·窗口 {c['action_window']}]")
            if c.get("entry_price"):
                lines.append(f"   买点 {c['entry_price']:.2f} / 止损 {c['stop_price']:.2f} / 止盈 +{c['target_pct']:.1f}%")
            lines.append(f"   仓位：≤{c.get('size_cap', 25)}%")
    else:
        lines += ["", "📌 无盘中可下单候选：已过安全买入窗口或缺少可执行买点，记录主线，明日 L1 复核"]
    lines += ["", "📌 下次 intraday 时点强制复核"]
    text = "\n".join(lines)
    if not _should_push("T2"):
        log.info("[%s push T2 %s] %s", PUSH_LEVEL.upper(), concept, text[:100])
        return
    push(text, source="theme-loop")


def _should_push(level: str) -> bool:
    if PUSH_LEVEL == "shadow":
        return False
    if PUSH_LEVEL == "all":
        return True
    return PUSH_LEVEL == "t2" and level == "T2"


def pick_candidates(today: str, concept: str, signals: dict, now: datetime) -> list[dict]:
    """2-3 只候选 + 买卖纪律。leader 走 A 派接力（仅 1030 前），followers 走 D 派首板。"""
    members = signals.get("members") or []
    if not members:
        return []
    leader = signals.get("first_leader")
    candidates = []
    now_t = now.time()
    if now_t < dtime(10, 30):
        window = "before_1030"
    elif now_t < dtime(14, 0):
        window = "1030_1400"
    else:
        # ≥14:00 不追新主线（追高风险大），仅记录到 watchlist_dynamic 供明日 1 进 2 参考
        return []

    # Leader：A 派接力，仅当 leader 是首板且当前 < 10:30
    if leader and window == "before_1030" and (leader.get("limit_up_count") or 1) == 1:
        # 没有实时价格 → 买卖纪律用百分比描述
        candidates.append({
            "code": leader["code"], "name": leader["name"], "role": "leader",
            "entry_price": None, "stop_price": None, "target_pct": 5.0,
            "discipline_type": "A", "action_window": window, "size_cap": 30,
        })
    # Followers：板块内非 leader 的首板/二板，最多 2 只
    for m in members:
        if leader and m["code"] == leader["code"]:
            continue
        if (m.get("open_count") or 0) > 2:
            continue
        candidates.append({
            "code": m["code"], "name": m["name"], "role": "follower",
            "entry_price": None, "stop_price": None, "target_pct": 5.0,
            "discipline_type": "B" if window == "1030_1400" else "D",
            "action_window": window, "size_cap": 25,
        })
        if len(candidates) >= 3:
            break
    return candidates


def write_watchlist_dynamic(today: str, concept: str, candidates: list[dict],
                            source_id: int, now: datetime):
    with db_connect(DB) as conn:
        for c in candidates:
            conn.execute(
                """INSERT OR IGNORE INTO watchlist_dynamic
                   (trade_date, created_at, concept_tag, code, name, role,
                    entry_price, stop_price, target_pct,
                    discipline_type, action_window, source_emergence_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (today, now.isoformat(timespec="seconds"), concept,
                 c["code"], c["name"], c["role"],
                 c.get("entry_price"), c.get("stop_price"), c.get("target_pct"),
                 c["discipline_type"], c["action_window"], source_id))
        conn.commit()


# ─────────────────────────────────────────────────────
# PH state persistence
# ─────────────────────────────────────────────────────
def save_ph_state(today: str, detectors: dict[str, PageHinkley], now: datetime):
    """持久化 PH detector 状态（含 x_mean，否则重启后假触发）。"""
    with db_connect(DB) as conn:
        for tag, ph in detectors.items():
            s = ph.snapshot()
            conn.execute(
                """INSERT OR REPLACE INTO ph_state_snapshot
                   (trade_date, concept_tag, last_update, m_t, min_m_t, n_samples, x_mean)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (today, tag, now.isoformat(timespec="seconds"),
                 s["m_t"], s["min_m_t"], s["n"], s["x_mean"]))


def load_ph_state(today: str, detectors: dict[str, PageHinkley]) -> int:
    """从快照恢复，返回恢复的题材数。"""
    restored = 0
    with db_connect(DB) as conn:
        try:
            cur = conn.execute(
                "SELECT concept_tag, m_t, min_m_t, n_samples, x_mean FROM ph_state_snapshot WHERE trade_date=?",
                (today,))
            for tag, m, mn, n, xm in cur.fetchall():
                if tag not in detectors:
                    detectors[tag] = PageHinkley()
                detectors[tag].restore({"m_t": m, "min_m_t": mn, "n": n, "x_mean": xm or 0})
                restored += 1
        except Exception:
            log.exception("加载 PH 状态失败（旧 schema 无 x_mean 列？）")
    log.info("PH 状态从快照恢复 %d 个题材", restored)
    return restored


# ─────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────
def main_tick(now: datetime, today: str, wl: Whitelist,
              detectors: dict[str, PageHinkley],
              state: dict):
    """主 tick。state 持有跨 tick 的可变状态（consecutive_failures、tick_count）。"""
    if not in_session(now):
        return
    rows = fetch_anomaly_all(now)
    pool = fetch_zt_pool(today)
    if not rows and not pool:
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] == 5 and PUSH_LEVEL != "shadow":
            push(f"⚠️ theme_loop 数据源连续 5 tick 失联 · {now.strftime('%H:%M')}", source="theme-loop")
        return
    state["consecutive_failures"] = 0
    append_raw(now, rows)
    snapshot_zt_pool(now, today, pool, wl)

    # 聚合事件到题材
    events_by_concept = defaultdict(int)
    seen_concepts = set()
    for sym, code, name, t, info in rows:
        concept = map_to_concept(code, name, info, wl)
        if not concept:
            continue
        events_by_concept[concept] += 1
        seen_concepts.add(concept)

    # 喂 PH：每 tick 喂一次"该题材本 tick 事件计数"
    # 这样空窗 tick 喂 0 衰减，热点 tick 喂 N 累积
    for tag in seen_concepts:
        if tag not in detectors:
            detectors[tag] = PageHinkley()
    for tag, ph in detectors.items():
        ph.update(float(events_by_concept.get(tag, 0)))

    # PH 状态持久化节流（每 5 tick 一次）
    state["tick_count"] = state.get("tick_count", 0) + 1
    if state["tick_count"] % SAVE_PH_EVERY_N_TICKS == 0:
        save_ph_state(today, detectors, now)

    # 学习期：只喂数据，不触发
    if now.time() < LEARNING_END:
        return

    # 评估触发：所有 detector 都要查（P2-3 修复 — 不只看 seen_concepts，
    # 否则 T1 后该题材静默几 tick 就永远等不到 T2）
    for tag in list(detectors.keys()):
        ph = detectors[tag]
        signals = check_signals(today, tag, now, ph)
        ph_hit = signals["PH"]
        cluster_hit = signals["cluster3"]

        # T1
        if ph_hit and cluster_hit and not already_logged(today, tag, "T1"):
            sid = log_emergence(today, tag, "T1", signals, ph, now)
            try:
                push_t1_card(tag, signals, now)
            except Exception:
                log.exception("T1 推送失败: %s", tag)
            log.info("T1 触发 · %s · cluster=%d", tag, signals["cluster_count"])
            continue

        # T2
        t1_at = t1_triggered_at(today, tag)
        if t1_at and (now - t1_at).total_seconds() >= T2_PERSISTENCE_MIN * 60:
            bool_sigs = [signals["PH"], signals["cluster3"],
                         signals["first_seal_1030"], signals["second_board"]]
            if sum(bool_sigs) >= T2_MIN_SIGNALS and not already_logged(today, tag, "T2"):
                sid = log_emergence(today, tag, "T2", signals, ph, now)
                cands = pick_candidates(today, tag, signals, now)
                if cands:
                    write_watchlist_dynamic(today, tag, cands, sid, now)
                try:
                    push_t2_card(tag, signals, cands, now)
                except Exception:
                    log.exception("T2 推送失败: %s", tag)
                log.info("T2 触发 · %s · 候选 %d 只", tag, len(cands))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--push-level", choices=["shadow", "t2", "all"], default="t2",
                        help="推送级别：shadow 只写日志；t2 只推主线确认；all 推 T1+T2")
    parser.add_argument("--shadow", action="store_true",
                        help="兼容旧参数：等同 --push-level=shadow")
    args = parser.parse_args()

    global PUSH_LEVEL
    PUSH_LEVEL = "shadow" if args.shadow else args.push_level

    wl = load_whitelist()
    detectors: dict[str, PageHinkley] = {}
    state = {"consecutive_failures": 0, "tick_count": 0}
    today = datetime.now().strftime("%Y-%m-%d")
    load_ph_state(today, detectors)
    log.warning("theme_emergence_loop 启动 · interval=%ds · push_level=%s · 题材=%d",
                args.interval, PUSH_LEVEL, len(wl))

    if args.once:
        main_tick(datetime.now(), today, wl, detectors, state)
        return

    while True:
        now = datetime.now()
        # 15:00 之后退出
        if now.time() > dtime(15, 0):
            log.info("已过 15:00，daemon 退出")
            break
        # 跨日重置（极少触发，daemon 一般不跨日；保险起见保留）
        cur_date = now.strftime("%Y-%m-%d")
        if cur_date != today:
            log.info("跨日重置：%s → %s", today, cur_date)
            detectors.clear()
            today = cur_date
            state["tick_count"] = 0
        try:
            main_tick(now, today, wl, detectors, state)
        except Exception:
            log.exception("tick 异常，本轮跳过")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

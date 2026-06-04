"""
盘中新主线浮现识别 daemon · Layer 1。

每 60 秒一个 tick：
  ① 拉 ak.stock_changes_em（4 类异动）+ ak.stock_zt_pool_em（涨停池）
     全量异动先写共享唯一事件库，再按 theme-loop 独立游标消费
  ② 每 5 分钟采集紧凑市场快照，异动流每分钟映射到题材图谱
  ③ 五维评分状态机触发 T0 / T1 / T2 / 降温 / 轮出
  ④ T1 / T2 继续写 theme_emergence_log 兼容审计
  ⑤ 状态迁移写事件队列并按冷却纪律推即时短讯

监控时段：09:30-11:30 / 13:00-15:00
T1 最早触发：09:35

数据落盘：
  data/anomaly_raw/{date}.jsonl  首次入库的全市场异动事件审计副本
  data/daily.db                  5 张新表（见 init_db.sql）

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

from stock_codex.infra.db import connect_close as db_connect  # noqa: E402 — daemon 用自动关闭版
from stock_codex.infra.logger import get_logger, init_req_id_from_env  # noqa: E402
from stock_codex.infra.push_wrapper import push_one  # noqa: E402
from stock_codex.market import anomaly_events  # noqa: E402
from stock_codex.market.market_snapshot import MarketSnapshot  # noqa: E402
from stock_codex.market.theme_candidates import ThemeCandidateEngine  # noqa: E402
from stock_codex.market.theme_graph import ThemeGraph  # noqa: E402
from stock_codex.market.theme_signal import ThemeSignal  # noqa: E402
from stock_codex.paths import DATA_DIR, DB_FILE

init_req_id_from_env()
log = get_logger("theme_emergence_loop")

DB = DB_FILE
WHITELIST = DATA_DIR / "concept_whitelist.yaml"
RAW_DIR = DATA_DIR / "anomaly_raw"

SESSION_AM = (dtime(9, 30), dtime(11, 30))
SESSION_PM = (dtime(13, 0), dtime(15, 0))
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


def load_whitelist() -> ThemeGraph:
    if not WHITELIST.exists():
        log.warning("本地 concept_whitelist.yaml 不存在，使用随代码发布的默认题材目录")
    graph = ThemeGraph(WHITELIST, db_path=DB)
    member_count = sum(len(graph.member_records(theme)) for theme in graph.themes)
    log.info("题材图谱 %d 题材 / %d 成员", len(graph), member_count)
    return graph


def map_to_concept(
    code: str,
    name: str,
    info: str,
    wl: ThemeGraph | Whitelist,
    *,
    sector_hint: str = "",
    as_of: datetime | None = None,
) -> str | None:
    """兼容旧调用：返回题材图谱主标签。"""
    if isinstance(wl, ThemeGraph):
        matches = wl.resolve(code, name, sector_hint, info, as_of or datetime.now())
        primary = next((match for match in matches if match.is_primary), None)
        return primary.theme if primary else None
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
    """拉全市场异动事件，返回共享事件库可直接写入的 dict。"""
    import akshare as ak
    rows = []
    for sym in ANOMALY_SYMBOLS:
        for attempt in range(3):
            try:
                df = ak.stock_changes_em(symbol=sym)
                for _, r in df.iterrows():
                    rows.append({
                        "symbol": sym,
                        "code": str(r["代码"]),
                        "name": r["名称"],
                        "event_time": str(r.get("时间", "")),
                        "info": str(r.get("相关信息", "")),
                        "sector_hint": str(r.get("板块", "")),
                    })
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

def snapshot_zt_pool(now: datetime, today: str, pool: list, wl: ThemeGraph | Whitelist):
    if not pool:
        return
    ts = now.isoformat(timespec="seconds")
    with db_connect(DB) as conn:
        for r in pool:
            code = str(r.get("代码", "")).zfill(6)
            # 用所属行业 + 涨停统计作为额外 keyword 语料
            extra = f"{r.get('所属行业', '')} {r.get('涨停统计', '')}"
            concept = map_to_concept(code, r.get("名称", ""), extra, wl, as_of=now)
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
    push_one(text, source="theme-loop")


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
    push_one(text, source="theme-loop")


def _should_push(level: str) -> bool:
    if PUSH_LEVEL == "shadow":
        return False
    if PUSH_LEVEL == "all":
        return True
    return PUSH_LEVEL == "t2" and level == "T2"


def pick_candidates(today: str, concept: str, signals: dict, now: datetime) -> list[dict]:
    """旧 T2 只掌握涨停池成员，不能据此生成自动交易候选。"""
    return []


def recent_theme_events(
    now: datetime,
    wl: ThemeGraph | Whitelist,
    *,
    window_minutes: int = 5,
) -> dict[str, list[dict]]:
    """读取最近窗口内已首次入库的唯一事件，并按图谱做多标签归因。"""
    cutoff = (now - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    today = now.strftime("%Y-%m-%d")
    with db_connect(DB) as conn:
        cur = conn.execute(
            """SELECT id, event_key, observed_at, event_time, symbol, code, name,
                      sector_hint, info
               FROM anomaly_event
               WHERE trade_date=? AND observed_at>=? AND observed_at<=?
               ORDER BY id""",
            (today, cutoff, now.isoformat(timespec="seconds")),
        )
        cols = [col[0] for col in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    out: dict[str, list[dict]] = defaultdict(list)
    for event in rows:
        if isinstance(wl, ThemeGraph):
            matches = wl.resolve(
                event["code"],
                event["name"],
                event.get("sector_hint", ""),
                event["info"],
                now,
            )
            concepts = [match.theme for match in matches]
        else:
            concept = map_to_concept(event["code"], event["name"], event["info"], wl)
            concepts = [concept] if concept else []
        for concept in concepts:
            out[concept].append(event)
    return dict(out)


def latest_limit_up_counts(today: str) -> dict[str, int]:
    with db_connect(DB) as conn:
        rows = conn.execute(
            """SELECT concept_top1, COUNT(DISTINCT code)
               FROM intraday_limit_up_snapshot
               WHERE trade_date=? AND concept_top1 IS NOT NULL
                 AND snapshot_ts=(
                     SELECT MAX(snapshot_ts) FROM intraday_limit_up_snapshot
                     WHERE trade_date=?
                 )
               GROUP BY concept_top1""",
            (today, today),
        ).fetchall()
    return {str(theme): int(count) for theme, count in rows}


def log_score_emergence(today: str, transition: dict, now: datetime, limit_up_count: int) -> None:
    """把 v2 T1/T2 状态迁移写入旧表，维持现有校准工具兼容。"""
    event_type = transition["event_type"]
    if event_type not in {"T1", "T2"}:
        return
    levels = ["T1", "T2"] if event_type == "T2" else ["T1"]
    for level in levels:
        if already_logged(today, transition["theme"], level):
            continue
        with db_connect(DB) as conn:
            conn.execute(
                """INSERT INTO theme_emergence_log
                   (detected_at, trade_date, concept_tag, signal_level, signals_hit,
                    cluster_count, first_leader, ph_value, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now.isoformat(timespec="seconds"),
                    today,
                    transition["theme"],
                    level,
                    json.dumps(transition["components"], ensure_ascii=False),
                    limit_up_count,
                    transition.get("primary_anchor"),
                    transition["score"],
                    "theme_signal_v2",
                ),
            )


def format_state_short(transition: dict, now: datetime) -> str:
    labels = {
        "T0": "题材观察",
        "T1": "主线浮现",
        "T2": "主线确认",
        "cooling": "题材降温",
        "rotation": "主线轮出",
    }
    components = transition["components"]
    lines = [
        f"📍 [{now.strftime('%H:%M')}] {labels[transition['event_type']]} · {transition['theme']}",
        f"评分 {transition['score']:.0f} · 异动 {components['anomaly_flow']:.0f} / "
        f"广度 {components['breadth']:.0f} / 锚点 {components['anchor']:.0f} / "
        f"催化 {components['catalyst']:.0f} / 涨停确认 {components['confirmation']:.0f}",
    ]
    if transition.get("primary_anchor"):
        lines.append(f"锚点：{transition['primary_anchor']}")
    if transition["event_type"] in {"T0", "T1", "T2"}:
        lines.append("仅作题材状态提醒，不追涨停股；等待可执行买点。")
    return "\n".join(lines)


def handle_state_transitions(
    signal_engine: ThemeSignal,
    transitions: list[dict],
    now: datetime,
    limit_up_counts: dict[str, int],
) -> None:
    today = now.strftime("%Y-%m-%d")
    for transition in transitions:
        log_score_emergence(
            today,
            transition,
            now,
            limit_up_counts.get(transition["theme"], 0),
        )
        if not _should_push(transition["event_type"]):
            continue
        if not signal_engine.can_push_short(transition["id"], now):
            continue
        try:
            push_one(format_state_short(transition, now), source="theme-loop")
            signal_engine.mark_short_pushed(transition["id"], now)
        except Exception:
            log.exception("状态短讯推送失败: %s %s", transition["theme"], transition["event_type"])


def handle_candidate_transitions(
    candidate_engine: ThemeCandidateEngine,
    transitions: list[dict],
    evaluations: list,
    market_snapshot: dict,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    """T1 持续尝试生成趋势票，并按状态迁移或价格/截止时间失效。"""
    added: list[dict] = []
    invalidated: list[dict] = []
    t1_sources: dict[str, str] = {}
    for transition in transitions:
        event_type = transition["event_type"]
        theme = transition["theme"]
        if event_type == "T1":
            t1_sources[theme] = f"market_state_event:{transition['id']}"
        elif event_type in {"cooling", "rotation"}:
            reason = "题材降温" if event_type == "cooling" else "题材轮出"
            invalidated.extend(candidate_engine.invalidate(theme, market_snapshot, now, reason=reason))

    for evaluation in evaluations:
        if getattr(evaluation, "state", None) == "T1":
            source_ref = t1_sources.get(
                evaluation.theme,
                f"theme_state_snapshot:{now.isoformat(timespec='seconds')}",
            )
            tickets = candidate_engine.build(
                evaluation.theme,
                "T1",
                market_snapshot,
                now,
                source_ref=source_ref,
            )
            added.extend(candidate_engine.write(tickets, now))
        invalidated.extend(candidate_engine.invalidate(evaluation.theme, market_snapshot, now))
    return added, invalidated


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
def main_tick(now: datetime, today: str, wl: ThemeGraph | Whitelist,
              detectors: dict[str, PageHinkley],
              state: dict):
    """主 tick。detectors 参数仅为旧调用兼容，v2 触发由 ThemeSignal 负责。"""
    if not in_session(now):
        return
    fetched_rows = fetch_anomaly_all(now)
    pool = fetch_zt_pool(today)
    if fetched_rows:
        anomaly_events.insert_events(DB, today, now, fetched_rows, raw_dir=RAW_DIR)
    rows = anomaly_events.read_new_events(DB, "theme-loop", today)
    if not fetched_rows and not pool and not rows:
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] == 5 and PUSH_LEVEL != "shadow":
            push_one(f"⚠️ theme_loop 数据源连续 5 tick 失联 · {now.strftime('%H:%M')}", source="theme-loop")
    else:
        state["consecutive_failures"] = 0
    snapshot_zt_pool(now, today, pool, wl)

    graph = wl if isinstance(wl, ThemeGraph) else ThemeGraph(WHITELIST, db_path=DB)
    snapshot_service = state.get("snapshot_service")
    if snapshot_service is None:
        snapshot_service = MarketSnapshot(DB, graph)
        state["snapshot_service"] = snapshot_service
    signal_engine = state.get("signal_engine")
    if signal_engine is None:
        signal_engine = ThemeSignal(DB)
        state["signal_engine"] = signal_engine
    candidate_engine = state.get("candidate_engine")
    if candidate_engine is None:
        candidate_engine = ThemeCandidateEngine(DB, graph)
        state["candidate_engine"] = candidate_engine

    market_snapshot = snapshot_service.capture(now)
    events_by_theme = recent_theme_events(now, wl)
    limit_up_counts = latest_limit_up_counts(today)
    evaluations, transitions = signal_engine.evaluate(
        now,
        market_snapshot,
        events_by_theme,
        limit_up_counts=limit_up_counts,
    )
    state["latest_market_snapshot"] = market_snapshot
    state["latest_evaluations"] = evaluations
    added, invalidated = handle_candidate_transitions(
        candidate_engine,
        transitions,
        evaluations,
        market_snapshot,
        now,
    )
    state["latest_candidates_added"] = added
    state["latest_candidates_invalidated"] = invalidated
    handle_state_transitions(signal_engine, transitions, now, limit_up_counts)
    for transition in transitions:
        log.info(
            "%s · %s · score=%.0f",
            transition["event_type"],
            transition["theme"],
            transition["score"],
        )
    if added or invalidated:
        log.info("候选新增 %d / 失效 %d", len(added), len(invalidated))
    if rows:
        anomaly_events.advance_cursor(DB, "theme-loop", today, rows[-1]["id"])


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
    state = {"consecutive_failures": 0}
    today = datetime.now().strftime("%Y-%m-%d")
    anomaly_events.ensure_schema(DB)
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
        try:
            main_tick(now, today, wl, detectors, state)
        except Exception:
            log.exception("tick 异常，本轮跳过")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

# T+1 Awareness · Batch 1 · 数据基础 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 T+1 改造打基础——交易日历 + holdings 状态机 + schema 演进，本批次合入后系统行为不变，仅新增可调用工具与字段。

**Architecture:** 新增 `code/lib/` 目录承载 calendar / holdings 两个纯工具模块；`holdings.yaml` 向后兼容扩字段（`unlock_date` / `source`）；refresh 脚本拉 akshare 交易日历落地为 csv；既有 `fetch_realtime.load_holdings` 改为薄包装 `code/lib/holdings.read_holdings`。

**Tech Stack:** Python 3.11+, pyyaml, akshare, pytest（新增）, filelock（新增依赖）。

**Spec reference:** `docs/superpowers/specs/2026-05-14-t-plus-1-awareness-design.md` §4 / §5.1 / §5.2 / §10 批次 1。

**Field mapping note:** spec 用 `school`，实际 yaml 现状字段名为 `genre`——本 plan 一律沿用 `genre`，避免破坏既有 watch_loop 评估逻辑。

---

## File Structure

| File | Responsibility | New / Modify |
|---|---|---|
| `pyproject.toml` | 加 pytest / filelock 依赖 | Modify |
| `code/lib/__init__.py` | 包初始化 | Create |
| `code/lib/calendar.py` | 交易日历查询 `is_trade_day` / `next_trade_day` / `trade_days_between` | Create |
| `code/lib/holdings.py` | `Holding` dataclass、`read_holdings`、`upsert_holding`、`remove_holding` | Create |
| `code/refresh_calendar.py` | 拉 akshare 交易日历刷新本地 csv | Create |
| `data/trade_calendar.csv` | 交易日单列表（1990-01-01 起，覆盖至次年底） | Create (bootstrap) |
| `tests/__init__.py` | 测试包初始化 | Create |
| `tests/test_calendar.py` | 日历模块单测 | Create |
| `tests/test_holdings.py` | holdings 模块单测 | Create |
| `holdings.yaml` | header 注释加新字段说明 | Modify |
| `.claude/skills/stock-intraday/scripts/fetch_realtime.py:103-109` | `load_holdings` 改为转调新模块 | Modify |

---

### Task 1: 项目准备 · 加测试 + 依赖

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `code/lib/__init__.py`

- [ ] **Step 1: 在 pyproject.toml `[project] dependencies` 加 filelock，新增 dev 依赖 + pytest 配置**

修改 `pyproject.toml`，在 `dependencies` 列表末尾追加 `"filelock>=3.13"`，并在文件末尾追加：

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
]

[tool.pytest.ini_options]
pythonpath = ["code"]
testpaths = ["tests"]
```

> **为什么 `pythonpath = ["code"]`**：项目既有约定是把 `code/` 作为 sys.path 入口（见 watch_loop.py 第 30 行），所有模块以 `lib.x` / `notify` 等扁平名导入。pytest 配置同步该约定，所有测试和生产代码用同一套 import path。

- [ ] **Step 2: 创建空 init 文件**

```bash
mkdir -p code/lib tests
touch code/lib/__init__.py tests/__init__.py
```

- [ ] **Step 3: 同步依赖**

Run: `uv sync --group dev`
Expected: filelock + pytest 安装成功，无报错。

- [ ] **Step 4: 验证 pytest 可跑**

Run: `uv run pytest --collect-only tests/ 2>&1 | tail -3`
Expected: `collected 0 items` 或类似（目录存在但没用例）。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock code/lib/__init__.py tests/__init__.py
git commit -m "chore: 加 pytest / filelock 依赖 + code/lib 包骨架"
```

---

### Task 2: 交易日历 · TDD 实现 calendar.py

**Files:**
- Create: `tests/test_calendar.py`
- Create: `code/lib/calendar.py`

- [ ] **Step 1: 写失败用例**

Create `tests/test_calendar.py`：

```python
from datetime import date
from pathlib import Path
import pytest

from lib import calendar as cal


@pytest.fixture
def tiny_csv(tmp_path: Path, monkeypatch) -> Path:
    """造一个最小日历：2026-05-14 (周四) / 05-15 (周五) / 05-18 (周一)，
    跳过 05-16 / 05-17 周末。"""
    p = tmp_path / "trade_calendar.csv"
    p.write_text("trade_date\n2026-05-14\n2026-05-15\n2026-05-18\n", encoding="utf-8")
    monkeypatch.setattr(cal, "CALENDAR_FILE", p)
    cal._cache_clear()
    return p


def test_is_trade_day_true(tiny_csv):
    assert cal.is_trade_day(date(2026, 5, 14)) is True


def test_is_trade_day_false_weekend(tiny_csv):
    assert cal.is_trade_day(date(2026, 5, 16)) is False


def test_next_trade_day_skips_weekend(tiny_csv):
    # 周五 buy → 下一交易日是周一
    assert cal.next_trade_day(date(2026, 5, 15)) == date(2026, 5, 18)


def test_next_trade_day_from_non_trade(tiny_csv):
    # 周六问下一交易日，仍是周一
    assert cal.next_trade_day(date(2026, 5, 16)) == date(2026, 5, 18)


def test_trade_days_between(tiny_csv):
    # 05-14 到 05-18 之间含 14/15/18 三个交易日，间隔 = 2
    assert cal.trade_days_between(date(2026, 5, 14), date(2026, 5, 18)) == 2


def test_next_trade_day_out_of_range_raises(tiny_csv):
    # 日历只到 05-18，问 05-19 之后没数据 → 抛错而不是返回 None
    with pytest.raises(cal.CalendarOutOfRange):
        cal.next_trade_day(date(2026, 5, 18))
```

- [ ] **Step 2: 运行验证失败**

Run: `uv run pytest tests/test_calendar.py -v`
Expected: ImportError / ModuleNotFoundError on `code.lib.calendar`.

- [ ] **Step 3: 实现 calendar.py**

Create `code/lib/calendar.py`：

```python
"""交易日历查询。数据来源：data/trade_calendar.csv（由 refresh_calendar.py 维护）。"""
from __future__ import annotations
import bisect
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALENDAR_FILE = ROOT / "data" / "trade_calendar.csv"


class CalendarOutOfRange(Exception):
    """请求的日期超出本地日历覆盖范围。"""


@lru_cache(maxsize=1)
def _load() -> list[date]:
    if not CALENDAR_FILE.exists():
        raise CalendarOutOfRange(f"交易日历文件不存在：{CALENDAR_FILE}，请先跑 refresh_calendar.py")
    days: list[date] = []
    for i, line in enumerate(CALENDAR_FILE.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or i == 0:  # 跳过 header
            continue
        days.append(datetime.strptime(line, "%Y-%m-%d").date())
    return sorted(days)


def _cache_clear() -> None:
    """测试用：刷新缓存。"""
    _load.cache_clear()


def is_trade_day(d: date) -> bool:
    days = _load()
    idx = bisect.bisect_left(days, d)
    return idx < len(days) and days[idx] == d


def next_trade_day(d: date) -> date:
    """返回严格大于 d 的下一个交易日。"""
    days = _load()
    idx = bisect.bisect_right(days, d)
    if idx >= len(days):
        raise CalendarOutOfRange(
            f"日历覆盖到 {days[-1]}，无法回答 {d} 之后的下一交易日；请刷新日历"
        )
    return days[idx]


def trade_days_between(a: date, b: date) -> int:
    """[a, b] 闭区间内交易日数 - 1。a==b 且是交易日返回 0。"""
    if a > b:
        a, b = b, a
    days = _load()
    lo = bisect.bisect_left(days, a)
    hi = bisect.bisect_right(days, b)
    n = hi - lo
    return max(n - 1, 0)
```

- [ ] **Step 4: 运行测试**

Run: `uv run pytest tests/test_calendar.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add code/lib/calendar.py tests/test_calendar.py
git commit -m "feat(lib/calendar): 交易日历查询模块 + 单测"
```

---

### Task 3: 交易日历 · refresh 脚本 + bootstrap 数据

**Files:**
- Create: `code/refresh_calendar.py`
- Create: `data/trade_calendar.csv`（脚本生成）

- [ ] **Step 1: 写脚本**

Create `code/refresh_calendar.py`：

```python
"""幂等刷新本地交易日历。
数据源：akshare.tool_trade_date_hist_sina()（返回 1990 至次年底全部 A 股交易日）。
失败时不删除现有 csv，仅打印错误并退非零码。
"""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

import akshare as ak  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "trade_calendar.csv"


def main() -> int:
    CSV.parent.mkdir(parents=True, exist_ok=True)
    try:
        df = ak.tool_trade_date_hist_sina()
    except Exception as e:
        print(f"[refresh_calendar] akshare 拉取失败：{e}", file=sys.stderr)
        return 1

    # akshare 返回列名为 trade_date，类型可能是 datetime.date 或字符串
    col = "trade_date"
    if col not in df.columns:
        print(f"[refresh_calendar] 接口返回无 {col} 列，实际列：{list(df.columns)}", file=sys.stderr)
        return 2

    dates = sorted({_to_date(v) for v in df[col]})
    lines = ["trade_date"] + [d.isoformat() for d in dates]
    CSV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[refresh_calendar] 写入 {len(dates)} 个交易日 → {CSV} (最新 {dates[-1]})")
    return 0


def _to_date(v) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 跑一次 bootstrap 数据**

Run: `uv run python code/refresh_calendar.py`
Expected: 输出 `写入 N 个交易日 → ... (最新 YYYY-MM-DD)`，N 在 8000-9000 之间，最新日期 ≥ 2026-12-30。

- [ ] **Step 3: 抽检数据**

Run: `head -3 data/trade_calendar.csv && tail -3 data/trade_calendar.csv && wc -l data/trade_calendar.csv`
Expected: 第一行 `trade_date`，第二行起为 ISO 日期；总行数 ~8500。

- [ ] **Step 4: 跑一次 calendar 实测**

Run: `uv run python -c "import sys; sys.path.insert(0, 'code'); from datetime import date; from lib import calendar as c; print(c.next_trade_day(date(2026, 5, 14)))"`
Expected: 输出 `2026-05-15`（若 05-15 是交易日；否则下一个交易日）。

- [ ] **Step 5: Commit**

```bash
git add code/refresh_calendar.py data/trade_calendar.csv
git commit -m "feat(refresh_calendar): 拉 akshare 交易日历 + bootstrap data/trade_calendar.csv"
```

---

### Task 4: holdings 状态机 · TDD 实现 holdings.py

**Files:**
- Create: `tests/test_holdings.py`
- Create: `code/lib/holdings.py`

- [ ] **Step 1: 写失败用例**

Create `tests/test_holdings.py`：

```python
from datetime import date
from pathlib import Path
import pytest
import yaml

from lib import calendar as cal
from lib import holdings as h


@pytest.fixture
def tmp_yaml(tmp_path: Path, monkeypatch) -> Path:
    """指向 tmp 的 holdings.yaml，并装一个 mini 日历支持 2026-05-14 / 05-15。"""
    cal_csv = tmp_path / "trade_calendar.csv"
    cal_csv.write_text("trade_date\n2026-05-14\n2026-05-15\n2026-05-18\n", encoding="utf-8")
    monkeypatch.setattr(cal, "CALENDAR_FILE", cal_csv)
    cal._cache_clear()

    yml = tmp_path / "holdings.yaml"
    yml.write_text("holdings: []\n", encoding="utf-8")
    monkeypatch.setattr(h, "HOLDINGS_FILE", yml)
    return yml


def test_read_empty(tmp_yaml):
    assert h.read_holdings() == []


def test_upsert_new(tmp_yaml):
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.02, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.90, take_profit=9.50,
        source="manual",
    )
    h.upsert_holding(rec)
    got = h.read_holdings()
    assert len(got) == 1
    assert got[0].code == "000601"
    assert got[0].unlock_date == date(2026, 5, 15)  # next trade day after 05-14


def test_upsert_merge_weighted_avg(tmp_yaml):
    """同 code 加仓：cost 加权均价，shares 累加，unlock_date 取最新一笔。"""
    base = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    addon = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.5, shares=1000,
        buy_date=date(2026, 5, 15),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(base)
    h.upsert_holding(addon)
    got = h.read_holdings()
    assert len(got) == 1
    merged = got[0]
    assert merged.shares == 2000
    assert merged.cost == pytest.approx(9.25)  # (9.0*1000 + 9.5*1000) / 2000
    assert merged.unlock_date == date(2026, 5, 18)  # next trade day after 05-15


def test_is_locked(tmp_yaml):
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(rec)
    got = h.read_holdings()[0]
    assert got.is_locked(date(2026, 5, 14)) is True
    assert got.is_locked(date(2026, 5, 15)) is False  # unlock_date 当日已解锁
    assert got.is_locked(date(2026, 5, 18)) is False


def test_remove_holding(tmp_yaml):
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(rec)
    removed = h.remove_holding("000601")
    assert removed.code == "000601"
    assert h.read_holdings() == []


def test_remove_missing_raises(tmp_yaml):
    with pytest.raises(KeyError):
        h.remove_holding("999999")


def test_legacy_record_without_unlock_date(tmp_yaml):
    """老条目缺 unlock_date / source：视为已解锁、source=manual。"""
    tmp_yaml.write_text(yaml.safe_dump({
        "holdings": [{
            "code": "600000", "name": "浦发银行", "genre": "C",
            "cost": 10.0, "shares": 500,
            "buy_date": "2026-04-01",
            "stop_loss": 9.5, "take_profit": 11.0,
            "note": "历史持仓",
        }]
    }), encoding="utf-8")
    got = h.read_holdings()
    assert len(got) == 1
    assert got[0].unlock_date == date(2026, 4, 1)  # buy_date 兜底，等价已解锁
    assert got[0].is_locked(date(2026, 5, 14)) is False
    assert got[0].source == "manual"


def test_atomic_write(tmp_yaml):
    """写入过程中 yaml 不应出现半截内容（原子 rename 测试）。"""
    rec = h.Holding(
        code="000601", name="韶能股份", genre="B",
        cost=9.0, shares=1000,
        buy_date=date(2026, 5, 14),
        stop_loss=8.9, take_profit=None, source="manual",
    )
    h.upsert_holding(rec)
    # 文件应能被 yaml 完整解析（没有半截）
    parsed = yaml.safe_load(tmp_yaml.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict) and "holdings" in parsed
```

- [ ] **Step 2: 运行验证失败**

Run: `uv run pytest tests/test_holdings.py -v`
Expected: ImportError on `code.lib.holdings`.

- [ ] **Step 3: 实现 holdings.py**

Create `code/lib/holdings.py`：

```python
"""持仓状态机。读写 holdings.yaml，提供 Holding dataclass 与 is_locked 状态。

并发控制：filelock + 原子 rename。watch_loop 读、bot_inbound 写，互不阻塞主流程。
"""
from __future__ import annotations
import os
import tempfile
from dataclasses import dataclass, asdict, field
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from filelock import FileLock

from lib import calendar as cal

ROOT = Path(__file__).resolve().parents[2]
HOLDINGS_FILE = ROOT / "holdings.yaml"
LOCK_FILE = ROOT / "holdings.yaml.lock"


@dataclass
class Holding:
    code: str
    name: str
    genre: str  # A / B / C / D
    cost: float
    shares: int
    buy_date: date
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    unlock_date: Optional[date] = None
    source: str = "manual"
    note: str = ""

    def __post_init__(self) -> None:
        if self.unlock_date is None:
            # 兜底：尝试用 calendar 算；算不出（历史日期超出范围）则取 buy_date 视为已解锁
            try:
                self.unlock_date = cal.next_trade_day(self.buy_date)
            except cal.CalendarOutOfRange:
                self.unlock_date = self.buy_date

    def is_locked(self, today: date) -> bool:
        return today < self.unlock_date

    def to_yaml_dict(self) -> dict:
        d = asdict(self)
        d["buy_date"] = self.buy_date.isoformat()
        d["unlock_date"] = self.unlock_date.isoformat() if self.unlock_date else None
        # 移除空字段以保持 yaml 简洁
        return {k: v for k, v in d.items() if v not in (None, "", 0) or k in ("cost", "shares")}


def _from_yaml_dict(d: dict) -> Holding:
    buy_date = _parse_date(d["buy_date"])
    unlock_date = _parse_date(d["unlock_date"]) if d.get("unlock_date") else None
    return Holding(
        code=str(d["code"]),
        name=d["name"],
        genre=d.get("genre", "未标记"),
        cost=float(d["cost"]),
        shares=int(d.get("shares", 0)),
        buy_date=buy_date,
        stop_loss=float(d["stop_loss"]) if d.get("stop_loss") is not None else None,
        take_profit=float(d["take_profit"]) if d.get("take_profit") is not None else None,
        unlock_date=unlock_date,
        source=d.get("source", "manual"),
        note=d.get("note", ""),
    )


def _parse_date(v) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def read_holdings() -> list[Holding]:
    if not HOLDINGS_FILE.exists():
        return []
    with FileLock(str(LOCK_FILE)):
        raw = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) or {}
    items = raw.get("holdings") or []
    return [_from_yaml_dict(d) for d in items if d.get("code") and d.get("name")]


def upsert_holding(new: Holding) -> Holding:
    """新增或加仓。同 code 已存在则加权均价合并，unlock_date 取最新一笔。返回最终持仓记录。"""
    with FileLock(str(LOCK_FILE)):
        raw = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) if HOLDINGS_FILE.exists() else {}
        raw = raw or {}
        items = raw.get("holdings") or []
        existing_idx = next((i for i, d in enumerate(items) if str(d.get("code")) == new.code), None)
        if existing_idx is None:
            items.append(new.to_yaml_dict())
            final = new
        else:
            old = _from_yaml_dict(items[existing_idx])
            total_shares = old.shares + new.shares
            if total_shares == 0:
                raise ValueError("合并后 shares=0，不应触发 upsert")
            merged_cost = (old.cost * old.shares + new.cost * new.shares) / total_shares
            # unlock_date 取最大（最保守）
            merged_unlock = max(old.unlock_date, new.unlock_date)
            merged = Holding(
                code=new.code,
                name=new.name,
                genre=new.genre,
                cost=round(merged_cost, 4),
                shares=total_shares,
                buy_date=new.buy_date,  # 最新一笔的 buy_date
                stop_loss=new.stop_loss if new.stop_loss is not None else old.stop_loss,
                take_profit=new.take_profit if new.take_profit is not None else old.take_profit,
                unlock_date=merged_unlock,
                source=new.source,
                note=new.note or old.note,
            )
            items[existing_idx] = merged.to_yaml_dict()
            final = merged
        raw["holdings"] = items
        _atomic_write(raw)
    return final


def remove_holding(code: str) -> Holding:
    with FileLock(str(LOCK_FILE)):
        raw = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) if HOLDINGS_FILE.exists() else {}
        raw = raw or {}
        items = raw.get("holdings") or []
        idx = next((i for i, d in enumerate(items) if str(d.get("code")) == code), None)
        if idx is None:
            raise KeyError(f"持仓中无 {code}")
        removed = _from_yaml_dict(items.pop(idx))
        raw["holdings"] = items
        _atomic_write(raw)
    return removed


def _atomic_write(raw: dict) -> None:
    HOLDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".holdings.", suffix=".yaml", dir=str(HOLDINGS_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, HOLDINGS_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
```

- [ ] **Step 4: 运行测试**

Run: `uv run pytest tests/test_holdings.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add code/lib/holdings.py tests/test_holdings.py
git commit -m "feat(lib/holdings): 持仓状态机 + Holding dataclass + is_locked + 加权均价合并"
```

---

### Task 5: 接入 fetch_realtime · 既有 load_holdings 改薄包装

**Files:**
- Modify: `.claude/skills/stock-intraday/scripts/fetch_realtime.py:103-109`

**目的**：消除两套 holdings 读取逻辑，保证 watch_loop 和盘前/盘后 skill 看到的字段完全一致；本步骤保持返回类型为 `list[dict]`，下游调用方暂不动。

- [ ] **Step 1: 替换 load_holdings 实现**

修改 `.claude/skills/stock-intraday/scripts/fetch_realtime.py` 第 103-109 行：

```python
def load_holdings() -> list[dict]:
    """转调 code/lib/holdings.read_holdings，保持 list[dict] 返回兼容下游。"""
    try:
        from lib.holdings import read_holdings  # noqa: WPS433 局部导入避免循环
    except ImportError as e:
        log(f"[warn] lib.holdings 不可用，回退旧读法：{e}")
        if not HOLDINGS_FILE.exists():
            log(f"[warn] holdings.yaml 不存在：{HOLDINGS_FILE}")
            return []
        data = yaml.safe_load(HOLDINGS_FILE.read_text(encoding="utf-8")) or {}
        out = data.get("holdings") or []
        return [h for h in out if h.get("code") and h.get("name") and h.get("cost")]
    holdings = read_holdings()
    # 转 dict，字段名沿用既有约定
    return [
        {
            "code": h.code, "name": h.name, "cost": h.cost, "shares": h.shares,
            "buy_date": h.buy_date.isoformat(), "genre": h.genre,
            "stop_loss": h.stop_loss, "take_profit": h.take_profit,
            "unlock_date": h.unlock_date.isoformat() if h.unlock_date else None,
            "source": h.source, "note": h.note,
        }
        for h in holdings
    ]
```

- [ ] **Step 2: 验证 watch_loop 仍能加载**

Run: `uv run python -c "import sys; sys.path.insert(0, '.claude/skills/stock-intraday/scripts'); from fetch_realtime import load_holdings; print(load_holdings())"`
Expected: 输出 `[]`（当前 holdings.yaml 为空）或既有持仓列表，无报错。

- [ ] **Step 3: 干跑一次 watch_loop --once**

Run: `uv run python .claude/skills/stock-intraday/scripts/watch_loop.py --once --no-raw 2>&1 | head -5`
Expected: 不报错。可能输出"无观察池 + 持仓，退出"或"非交易时段"——都正常。

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/stock-intraday/scripts/fetch_realtime.py
git commit -m "refactor(fetch_realtime): load_holdings 转调 code.lib.holdings 统一来源"
```

---

### Task 6: holdings.yaml header 注释更新

**Files:**
- Modify: `holdings.yaml`（仅 header 注释区，不动 `holdings: []`）

- [ ] **Step 1: 更新 header 字段说明**

修改 `holdings.yaml` 顶部注释（第 1-30 行），在"字段说明"段加入新字段：

```yaml
# 字段说明：
#   code        — 6 位股票代码（必填）
#   name        — 股票名称（必填）
#   cost        — 成本价（必填，含手续费均价；加仓时自动加权重算）
#   shares      — 持仓股数（必填）
#   buy_date    — 买入日期 YYYY-MM-DD（必填）；多次加仓存"最新一笔"日期
#   genre       — 派别：A=二板接力 / B=龙头补涨 / C=超跌反弹 / D=首板（必填）
#   stop_loss   — 硬止损价（必填，赵老哥派纪律）
#   take_profit — 目标止盈价（可选）
#   unlock_date — T+1 解锁日 YYYY-MM-DD（自动计算 = next_trade_day(buy_date)；
#                 today < unlock_date 视为锁仓中，watch_loop 告警走"明早预案"分轨）
#   source      — 录入来源：manual / watch_loop_buy_alert / bot_buy（默认 manual）
#   note        — 备注：题材 / L1 推荐时的买卖纪律摘要
#
# 历史条目兼容：老条目缺 unlock_date / source 时，read_holdings 会兜底为
#   unlock_date = buy_date（视为已解锁），source = manual，可放心保留。
```

- [ ] **Step 2: 验证 yaml 仍可解析**

Run: `uv run python -c "import yaml; print(yaml.safe_load(open('holdings.yaml')))"`
Expected: `{'holdings': []}` 或包含示例的字典，无 YAMLError。

- [ ] **Step 3: 跑全量测试**

Run: `uv run pytest tests/ -v`
Expected: 全部 14 个用例通过（calendar 6 + holdings 8）。

- [ ] **Step 4: Commit**

```bash
git add holdings.yaml
git commit -m "docs(holdings.yaml): header 补 unlock_date / source 字段说明 + 历史兼容说明"
```

---

### Task 7: 端到端冒烟 · 整批合入前最终验证

**Files:** 无修改，仅运行验证。

- [ ] **Step 1: 模拟一次完整买入流程**

Run:
```bash
uv run python -c "
import sys; sys.path.insert(0, 'code')
from datetime import date
from lib.holdings import Holding, upsert_holding, read_holdings, remove_holding
rec = Holding(code='999999', name='测试票', genre='B', cost=10.0, shares=100,
              buy_date=date.today(), stop_loss=9.5, take_profit=11.0, source='manual')
final = upsert_holding(rec)
print('UPSERTED:', final.code, 'unlock_date=', final.unlock_date, 'is_locked today=', final.is_locked(date.today()))
print('READ:', [h.code for h in read_holdings()])
removed = remove_holding('999999')
print('REMOVED:', removed.code)
print('AFTER:', read_holdings())
"
```
Expected:
- 第 1 行 `UPSERTED: 999999 unlock_date= 2026-XX-XX is_locked today= True`（unlock_date 是今天的下一交易日）
- 第 2 行 `READ: ['999999']`
- 第 3 行 `REMOVED: 999999`
- 第 4 行 `AFTER: []`

- [ ] **Step 2: 检查 holdings.yaml 已恢复空**

Run: `cat holdings.yaml | tail -5`
Expected: `holdings: []` 在末尾（注释保留）。如非空说明 remove 未生效，回查 Task 4。

- [ ] **Step 3: 跑 pytest 全集 + 检查无残留 lock 文件**

Run: `uv run pytest tests/ && ls holdings.yaml.lock 2>&1`
Expected: 全部通过；`.lock` 文件存在但是空的（filelock 不会自动清理 lock 文件，这是正常行为）。

- [ ] **Step 4: 把 lock 文件加入 .gitignore**

修改或新建 `.gitignore`，加一行 `holdings.yaml.lock`：

```bash
grep -q "^holdings.yaml.lock$" .gitignore 2>/dev/null || echo "holdings.yaml.lock" >> .gitignore
```

- [ ] **Step 5: Final commit**

```bash
git add .gitignore
git commit -m "chore: gitignore holdings.yaml.lock + Batch 1 数据基础完工"
```

---

## Batch 1 完成判定

完成本批次后，下列条件全部满足：

1. ✅ `uv run pytest tests/` 14 个用例全通过
2. ✅ `code/lib/calendar.py` 可独立查 `is_trade_day` / `next_trade_day`
3. ✅ `code/lib/holdings.py` 可读写 holdings.yaml + 加权均价合并
4. ✅ `data/trade_calendar.csv` 已 bootstrap 覆盖至次年底
5. ✅ `watch_loop.py` 仍能正常加载 holdings（行为无变化）
6. ✅ `holdings.yaml` header 注释已更新；老条目（如有）仍兼容

**本批次不会改变任何用户可见行为**——告警、skill 文案、推送内容全部保持现状。Batch 2 才开始用到这些工具。

## 下一步

Batch 1 合入并在生产跑 1-2 个交易日观察无异常后，调 writing-plans 生成 **Batch 2 · watch_loop 改造 plan**（sanity check + alert_router + pending_signals 落地）。

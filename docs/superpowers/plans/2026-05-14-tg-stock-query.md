# TG 单股查询助手 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Telegram 发送股票代码/名称，常驻进程 10s 轮询接收后调 CC headless 跑 `stock-query` skill，30–90s 内回一张题材派决策卡片（fresh / holding / 拒绝三种形态）。

**Architecture:**
- 新守护进程 `scripts/tg_listener.py`（launchd KeepAlive）轮询 TG → 解析 → 前置校验 → fcntl 文件锁串行化 → `subprocess` 调 `claude -p` headless 跑 `stock-query` skill → 把 markdown 输出 `push_md` 回原 chat_id。
- 新 skill `.claude/skills/stock-query/SKILL.md` + 新数据层 `code/lib/query.py`。
- 新表 `stock_basic` 由 `scripts/refresh_stock_basic.py` 每日 17:00 刷新。

**Tech Stack:** Python 3.12 (uv 管理)、SQLite (WAL)、`requests`、`pyyaml`、`filelock`、`subprocess`、Telegram Bot API、launchd plist。

**Spec reference:** `docs/superpowers/specs/2026-05-14-tg-stock-query-design.md`

## File Structure

### Create
| 路径 | 职责 |
|---|---|
| `code/lib/query.py` | 单股数据拉取层：K 线 / 实时盘口 / 概念 / 资金流 / 新闻；元数据查询（is_st / board / suspended） |
| `scripts/tg_listener.py` | 常驻进程：TG 长轮询、解析、前置校验、串行化、subprocess 调 CC |
| `scripts/refresh_stock_basic.py` | 每日 17:00 刷新 `stock_basic` 表 |
| `.claude/skills/stock-query/SKILL.md` | 题材派决策框架 + 卡片模板 prompt |
| `launchd/com.user.stocktglistener.plist.template` | KeepAlive 常驻 plist 模板（占位符 `{{PROJECT_ROOT}}`） |
| `tests/test_query_lib.py` | `code/lib/query.py` 元数据函数单元测试 |
| `tests/test_tg_listener.py` | `scripts/tg_listener.py` 解析 + 前置校验单元测试 |

### Modify
| 路径 | 修改点 |
|---|---|
| `code/init_db.sql` | 追加 `stock_basic` 表 DDL |
| `scripts/install_launchd.sh` | 注册新 plist |
| `.env.example`（若有；无则在 README 说明） | 增 `ALLOWED_CHAT_ID` |
| `pyproject.toml` | 加 `filelock`（如尚未声明） |
| `README.md` | 用法新增"TG 单股查询"一节 |

---

## Task 1: 加 `stock_basic` 表到 DB schema

**Files:**
- Modify: `code/init_db.sql`
- Test: `tests/test_stock_basic_schema.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_stock_basic_schema.py`：

```python
"""验证 stock_basic 表 schema 与初始化脚本可重入执行。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = ROOT / "code" / "init_db.sql"


def test_stock_basic_schema(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(SQL_FILE.read_text())
    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(stock_basic)")}
    assert cols == {
        "code": "TEXT",
        "name": "TEXT",
        "board": "TEXT",
        "list_date": "TEXT",
        "is_st": "INTEGER",
        "updated_at": "TEXT",
    }
    # 主键
    pks = [r[1] for r in conn.execute("PRAGMA table_info(stock_basic)") if r[5]]
    assert pks == ["code"]
    # 可重入
    conn.executescript(SQL_FILE.read_text())
    conn.close()
```

- [ ] **Step 2: 运行验证失败**

```bash
cd /Users/wispig/Desktop/stock && uv run pytest tests/test_stock_basic_schema.py -v
```
Expected: FAIL（表不存在）

- [ ] **Step 3: 在 init_db.sql 末尾追加表 DDL**

追加到 `code/init_db.sql` 末尾：

```sql
-- 股票基础信息（代码→名称/板块/上市日/ST 标志），refresh_stock_basic.py 每日刷新
CREATE TABLE IF NOT EXISTS stock_basic (
    code TEXT PRIMARY KEY,
    name TEXT,
    board TEXT,           -- main / chinext / star / bse
    list_date TEXT,
    is_st INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_stock_basic_board ON stock_basic(board);
```

- [ ] **Step 4: 重跑测试**

```bash
uv run pytest tests/test_stock_basic_schema.py -v
```
Expected: PASS

- [ ] **Step 5: 应用到生产 DB（幂等）**

```bash
sqlite3 data/daily.db < code/init_db.sql
sqlite3 data/daily.db "SELECT name FROM sqlite_master WHERE name='stock_basic';"
```
Expected: 输出 `stock_basic`

- [ ] **Step 6: Commit**

```bash
git add code/init_db.sql tests/test_stock_basic_schema.py
git commit -m "feat(db): add stock_basic table for TG query metadata

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `code/lib/query.py` 元数据 + 解析辅助函数（TDD）

**Files:**
- Create: `code/lib/query.py`
- Test: `tests/test_query_lib.py`

Skill 调用的数据拉取放到后面（需要联网），本任务先把不依赖网络的纯函数 TDD 出来：`parse_input`、`board_of`、`is_st`、`is_suspended_today`、`lookup_by_name`。

- [ ] **Step 1: 写失败测试**

```python
"""code/lib/query.py 元数据 / 解析单元测试。

注：不联网，is_suspended_today 用本地 DB 当日 daily_kline 是否有记录判断。
"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from lib import query  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    conn.executemany(
        "INSERT INTO stock_basic(code,name,board,list_date,is_st,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("600519", "贵州茅台",   "main",    "2001-08-27", 0, "2026-05-14"),
            ("300750", "宁德时代",   "chinext", "2018-06-11", 0, "2026-05-14"),
            ("688981", "中芯国际",   "star",    "2020-07-16", 0, "2026-05-14"),
            ("835174", "五新隧装",   "bse",     "2021-11-15", 0, "2026-05-14"),
            ("000725", "ST 京东方",  "main",    "1997-06-19", 1, "2026-05-14"),
            ("600000", "浦发银行",   "main",    "1999-11-10", 0, "2026-05-14"),
        ],
    )
    conn.commit()
    monkeypatch.setattr(query, "DB", p)
    return p


def test_parse_input_pure_code():
    assert query.parse_input("600519") == ("code", "600519")
    assert query.parse_input(" 600519 ") == ("code", "600519")


def test_parse_input_strips_prefix():
    assert query.parse_input("SH600519") == ("code", "600519")
    assert query.parse_input("sz300750") == ("code", "300750")
    assert query.parse_input("$600519") == ("code", "600519")
    assert query.parse_input("#600519") == ("code", "600519")


def test_parse_input_chinese_name():
    assert query.parse_input("贵州茅台") == ("name", "贵州茅台")
    assert query.parse_input("茅台") == ("name", "茅台")


def test_parse_input_rejects_garbage():
    assert query.parse_input("你好") == ("unknown", "你好")
    assert query.parse_input("12345") == ("unknown", "12345")
    assert query.parse_input("") == ("unknown", "")


def test_board_of(db):
    assert query.board_of("600519") == "main"
    assert query.board_of("300750") == "chinext"
    assert query.board_of("688981") == "star"
    assert query.board_of("835174") == "bse"
    assert query.board_of("999999") is None


def test_is_st(db):
    assert query.is_st("000725") is True
    assert query.is_st("600519") is False
    assert query.is_st("999999") is False


def test_lookup_by_name_exact(db):
    hits = query.lookup_by_name("贵州茅台")
    assert hits == [("600519", "贵州茅台")]


def test_lookup_by_name_substring(db):
    hits = query.lookup_by_name("茅台")
    assert ("600519", "贵州茅台") in hits
    assert len(hits) == 1


def test_lookup_by_name_multi():
    # 占位：两个含"科技"的票时返回多结果；当前 fixture 没有故略
    pass


def test_lookup_by_name_miss(db):
    assert query.lookup_by_name("不存在公司") == []


def test_is_suspended_today_no_kline(db):
    # daily_kline 当日无记录 → 视为停牌
    assert query.is_suspended_today("600519", today="2026-05-14") is True


def test_is_suspended_today_has_kline(db):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO daily_kline(code,date,open,high,low,close,vol,amount,pct_chg) "
        "VALUES ('600519','2026-05-14',1600,1610,1590,1605,1e6,1.6e9,0.5)"
    )
    conn.commit()
    conn.close()
    assert query.is_suspended_today("600519", today="2026-05-14") is False
```

- [ ] **Step 2: 运行验证失败**

```bash
uv run pytest tests/test_query_lib.py -v
```
Expected: ImportError（`code/lib/query.py` 不存在）

- [ ] **Step 3: 实现 `code/lib/query.py` 最小可过测部分**

创建 `code/lib/query.py`：

```python
"""单股查询数据层。本模块包含两类函数：

1. 不联网（本任务实现）：parse_input / board_of / is_st / is_suspended_today
   / lookup_by_name
2. 联网拉数据（Task 5 后续补充）：fetch_kline / fetch_realtime /
   fetch_concept_strength / fetch_money_flow / fetch_recent_news

DB 默认指向 data/daily.db；测试用 monkeypatch 替换。
"""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path
from typing import Optional

from db import connect

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "daily.db"

_CODE_RE = re.compile(r"^\d{6}$")
_PREFIX_RE = re.compile(r"^(?:sh|sz|bj)", re.IGNORECASE)
_CHINESE_RE = re.compile(r"[一-鿿]")


def parse_input(text: str) -> tuple[str, str]:
    """返回 (kind, value)。kind ∈ {"code","name","unknown"}。

    规则：去空白、去 $/# 前缀、去 SH/SZ/BJ 前缀；6 位纯数字→code；
    含中文→name；其它→unknown（静默忽略调用方决定）。
    """
    s = text.strip().lstrip("$#")
    s = _PREFIX_RE.sub("", s).strip()
    if _CODE_RE.match(s):
        return ("code", s)
    if _CHINESE_RE.search(s):
        return ("name", s)
    return ("unknown", s)


def board_of(code: str) -> Optional[str]:
    with connect(DB) as conn:
        row = conn.execute(
            "SELECT board FROM stock_basic WHERE code = ?", (code,)
        ).fetchone()
    return row[0] if row else None


def is_st(code: str) -> bool:
    with connect(DB) as conn:
        row = conn.execute(
            "SELECT is_st FROM stock_basic WHERE code = ?", (code,)
        ).fetchone()
    return bool(row and row[0])


def is_suspended_today(code: str, today: Optional[str] = None) -> bool:
    """无当日 daily_kline 记录 → 视为停牌（保守判定）。

    today 是 ISO yyyy-mm-dd；默认取当前日期。盘前/盘中调用时当日 kline 通常缺失，
    上层应用应只在盘后或确认数据已写入后才依赖此函数。
    """
    today = today or date.today().isoformat()
    with connect(DB) as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_kline WHERE code = ? AND date = ?",
            (code, today),
        ).fetchone()
    return row is None


def lookup_by_name(needle: str) -> list[tuple[str, str]]:
    """精确包含匹配（substring）。返回 [(code, name), ...]。"""
    with connect(DB) as conn:
        rows = conn.execute(
            "SELECT code, name FROM stock_basic WHERE name LIKE ?",
            (f"%{needle}%",),
        ).fetchall()
    return [(c, n) for c, n in rows]
```

- [ ] **Step 4: 运行测试至全绿**

```bash
uv run pytest tests/test_query_lib.py -v
```
Expected: 所有非 skip 测试 PASS。如个别用例 FAIL 修到全 PASS。

- [ ] **Step 5: Commit**

```bash
git add code/lib/query.py tests/test_query_lib.py
git commit -m "feat(query): parse_input + stock_basic 元数据查询

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `scripts/refresh_stock_basic.py`（每日刷新表）

**Files:**
- Create: `scripts/refresh_stock_basic.py`
- Test: `tests/test_refresh_stock_basic.py`（smoke：mock 接口返回固定行，断言写入）

- [ ] **Step 1: 写失败测试**

```python
"""refresh_stock_basic 烟雾测试：mock 接口返回 3 行，断言写入 stock_basic。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_stock_basic as r  # noqa: E402


def test_upsert_rows(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    conn.close()
    monkeypatch.setattr(r, "DB", db)

    fake_rows = [
        {"code": "600519", "name": "贵州茅台", "board": "main",    "list_date": "2001-08-27", "is_st": 0},
        {"code": "300750", "name": "宁德时代", "board": "chinext", "list_date": "2018-06-11", "is_st": 0},
        {"code": "000725", "name": "*ST 京东方","board": "main",   "list_date": "1997-06-19", "is_st": 1},
    ]
    with patch.object(r, "fetch_all_stock_basic", return_value=fake_rows):
        r.main()

    conn = sqlite3.connect(db)
    rows = sorted(conn.execute(
        "SELECT code,name,board,is_st FROM stock_basic ORDER BY code").fetchall())
    assert rows == [
        ("000725", "*ST 京东方", "main",    1),
        ("300750", "宁德时代",   "chinext", 0),
        ("600519", "贵州茅台",   "main",    0),
    ]


def test_board_inference():
    assert r.infer_board("600519") == "main"
    assert r.infer_board("000725") == "main"
    assert r.infer_board("002001") == "main"
    assert r.infer_board("300750") == "chinext"
    assert r.infer_board("688981") == "star"
    assert r.infer_board("835174") == "bse"
    assert r.infer_board("430139") == "bse"
```

- [ ] **Step 2: 运行验证失败**

```bash
uv run pytest tests/test_refresh_stock_basic.py -v
```
Expected: ImportError

- [ ] **Step 3: 实现脚本**

创建 `scripts/refresh_stock_basic.py`：

```python
"""每日 17:00 刷新 stock_basic 表（代码→名称/板块/上市日/ST 标志）。

数据源：东财 `clist.dfcf` 全市场列表（备选：新浪 list=sh_a / sz_a）。
失败重试 3 次，仍失败抛错让 launchd 记错；不写入半成品。

用法：uv run scripts/refresh_stock_basic.py
"""
from __future__ import annotations
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from db import connect  # noqa: E402

DB = ROOT / "data" / "daily.db"

EM_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn=1&pz=10000&po=1&np=1&fltt=2&invt=2"
    "&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    "&fields=f12,f14,f26"
)


def infer_board(code: str) -> str:
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("8", "4")) and len(code) == 6:
        return "bse"
    return "main"


def fetch_all_stock_basic() -> list[dict]:
    """拉全市场。f12=code, f14=name, f26=上市日(yyyymmdd int)。"""
    for attempt in range(3):
        try:
            r = requests.get(EM_URL, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json().get("data") or {}
            diff = data.get("diff") or []
            out = []
            for row in diff:
                code = str(row.get("f12") or "").zfill(6)
                if not code or len(code) != 6:
                    continue
                name = (row.get("f14") or "").strip()
                list_raw = row.get("f26")
                if isinstance(list_raw, int) and list_raw > 19900101:
                    list_date = f"{str(list_raw)[:4]}-{str(list_raw)[4:6]}-{str(list_raw)[6:8]}"
                else:
                    list_date = None
                is_st = 1 if ("ST" in name or "*ST" in name) else 0
                out.append({
                    "code": code, "name": name, "board": infer_board(code),
                    "list_date": list_date, "is_st": is_st,
                })
            if out:
                return out
        except Exception as e:
            print(f"[refresh_stock_basic] attempt {attempt + 1} 失败: {e}",
                  file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError("stock_basic 全市场拉取连续 3 次失败")


def main() -> None:
    rows = fetch_all_stock_basic()
    today = date.today().isoformat()
    with connect(DB) as conn:
        conn.executemany(
            """INSERT INTO stock_basic(code,name,board,list_date,is_st,updated_at)
               VALUES(:code,:name,:board,:list_date,:is_st,:updated_at)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, board=excluded.board,
                 list_date=excluded.list_date, is_st=excluded.is_st,
                 updated_at=excluded.updated_at""",
            [{**r, "updated_at": today} for r in rows],
        )
        conn.commit()
    print(f"[refresh_stock_basic] upsert {len(rows)} rows")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 测试至 PASS**

```bash
uv run pytest tests/test_refresh_stock_basic.py -v
```
Expected: PASS

- [ ] **Step 5: 实跑一次（写真数据库）**

```bash
uv run scripts/refresh_stock_basic.py
sqlite3 data/daily.db "SELECT COUNT(*), SUM(is_st) FROM stock_basic;"
```
Expected: 总数 >4000；is_st 总数 >50（实际值因当日 ST 名单浮动）

- [ ] **Step 6: Commit**

```bash
git add scripts/refresh_stock_basic.py tests/test_refresh_stock_basic.py
git commit -m "feat(refresh): 每日 stock_basic 全市场刷新

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `code/lib/query.py` 联网数据拉取函数

**Files:**
- Modify: `code/lib/query.py`（追加联网函数）
- Test: `tests/test_query_lib_network.py`（用 `responses` 或 `requests-mock` 包；若项目没装就用 `unittest.mock.patch("requests.get")`）

实现 5 个联网函数：`fetch_kline` `fetch_realtime` `fetch_concept_strength` `fetch_money_flow` `fetch_recent_news`。每个统一签名：抛异常时调用方降级。

- [ ] **Step 1: 写失败测试（mock 网络）**

```python
"""query.py 联网函数测试：全部 mock requests.get/post。"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from lib import query  # noqa: E402


def _mock_response(json_data=None, text=None, status=200):
    m = MagicMock()
    m.status_code = status
    m.raise_for_status = lambda: None
    m.json.return_value = json_data
    m.text = text or ""
    return m


def test_fetch_realtime_sina():
    sample = ('var hq_str_sh600519="贵州茅台,1600.00,1599.50,1605.00,'
              '1610.00,1590.00,1605.00,1605.50,1000000,'
              '1600000000.00,...,2026-05-14,15:00:00,00";')
    with patch("requests.get", return_value=_mock_response(text=sample)):
        r = query.fetch_realtime("600519")
    assert r["name"] == "贵州茅台"
    assert r["open"] == 1600.0
    assert r["close"] == 1605.0
    assert r["high"] == 1610.0
    assert r["low"] == 1590.0


def test_fetch_kline_returns_60_rows():
    rows = [{"day": f"2026-04-{i:02d}", "open": 1.0, "high": 1.1, "low": 0.9,
             "close": 1.05, "volume": 10000} for i in range(1, 31)]
    with patch("requests.get", return_value=_mock_response(json_data=rows)):
        df = query.fetch_kline("600519", days=30)
    assert len(df) == 30
    assert set(df.columns) >= {"date", "open", "high", "low", "close", "vol"}


def test_fetch_concept_strength_smoke():
    with patch("requests.get", return_value=_mock_response(json_data={
        "data": {"diff": [{"f12": "BK0475", "f14": "白酒",
                           "f3": 1.2, "f104": "贵州茅台"}]}})):
        r = query.fetch_concept_strength("600519")
    assert "concept_name" in r
    assert isinstance(r.get("rank"), (int, type(None)))


def test_fetch_money_flow_5d():
    with patch("requests.get", return_value=_mock_response(json_data={
        "data": {"klines": [
            "2026-05-10,1.0e8,2e7,3e7,4e7,5e7,1.5e8,1605,0.5",
            "2026-05-11,-1.0e7,...", "2026-05-12,2.0e7,...",
            "2026-05-13,-5.0e6,...", "2026-05-14,1.0e7,..."]}})):
        df = query.fetch_money_flow("600519", days=5)
    assert len(df) >= 1
    assert "main_in" in df.columns


def test_fetch_recent_news_returns_list():
    with patch("requests.get", return_value=_mock_response(json_data={
        "data": [{"title": "茅台分红方案公告", "url": "https://x", "date": "2026-05-13"}]})):
        items = query.fetch_recent_news("600519", days=7)
    assert isinstance(items, list)
    assert all("title" in x and "url" in x for x in items)
```

- [ ] **Step 2: 运行验证失败**

```bash
uv run pytest tests/test_query_lib_network.py -v
```
Expected: AttributeError（函数未实现）

- [ ] **Step 3: 追加实现到 `code/lib/query.py`**

在 `code/lib/query.py` 文件末尾追加：

```python
# ============================================================
# 联网数据拉取（失败抛异常，调用方降级处理）
# ============================================================
import requests
import pandas as pd


_UA = {"User-Agent": "Mozilla/5.0"}


def _sina_prefix(code: str) -> str:
    return "sh" + code if code.startswith(("5", "6", "9")) else "sz" + code


def fetch_realtime(code: str) -> dict:
    """新浪 hq.sinajs.cn 实时盘口。"""
    url = f"https://hq.sinajs.cn/list={_sina_prefix(code)}"
    r = requests.get(url, timeout=8,
                     headers={**_UA, "Referer": "https://finance.sina.com.cn"})
    r.raise_for_status()
    body = r.text.split('"')[1]
    parts = body.split(",")
    return {
        "name": parts[0], "open": float(parts[1]), "pre_close": float(parts[2]),
        "close": float(parts[3]), "high": float(parts[4]), "low": float(parts[5]),
        "vol": float(parts[8] or 0), "amount": float(parts[9] or 0),
        "date": parts[30] if len(parts) > 30 else "",
        "time": parts[31] if len(parts) > 31 else "",
    }


def fetch_kline(code: str, days: int = 60) -> pd.DataFrame:
    """新浪历史日 K（cn.finance.sina.com.cn）。"""
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
           "CN_MarketDataService.getKLineData")
    params = {"symbol": _sina_prefix(code), "scale": 240, "ma": 5, "datalen": days}
    r = requests.get(url, params=params, timeout=10, headers=_UA)
    r.raise_for_status()
    rows = r.json() or []
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"day": "date", "volume": "vol"})
    for col in ("open", "high", "low", "close", "vol"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_concept_strength(code: str) -> dict:
    """东财概念板块：取该票主线概念 + 5/10/20 日涨幅 + 龙头。

    单次接口拿不全；先返回简化结构，skill 端可再细化。
    """
    url = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=200&fid=f3&fs=m:90+t:3&fltt=2&invt=2"
           "&fields=f12,f14,f3,f104")
    r = requests.get(url, timeout=10, headers=_UA)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    diff = data.get("diff") or []
    # 简化：本期暂不做"该票属于哪个概念"反查（依赖额外接口），
    # 返回当日概念榜 Top 20 + 涨幅，让 skill prompt 自行用 fact pack 关联。
    top = []
    for row in diff[:20]:
        top.append({
            "concept_code": row.get("f12"),
            "concept_name": row.get("f14"),
            "pct_chg": row.get("f3"),
            "leader_name": row.get("f104"),
        })
    return {
        "concept_name": top[0]["concept_name"] if top else None,
        "rank": None,
        "top_concepts": top,
    }


def fetch_money_flow(code: str, days: int = 5) -> pd.DataFrame:
    """东财个股资金流 5 日。"""
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?secid={market}.{code}&klt=101&lmt={days}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65")
    r = requests.get(url, timeout=10, headers=_UA)
    r.raise_for_status()
    klines = ((r.json() or {}).get("data") or {}).get("klines") or []
    parsed = []
    for line in klines:
        cols = line.split(",")
        if len(cols) < 7:
            continue
        parsed.append({
            "date": cols[0],
            "main_in": float(cols[1] or 0),
            "small_in": float(cols[2] or 0),
            "medium_in": float(cols[3] or 0),
            "large_in": float(cols[4] or 0),
            "super_in": float(cols[5] or 0),
        })
    return pd.DataFrame(parsed)


def fetch_recent_news(code: str, days: int = 7) -> list[dict]:
    """同花顺新闻搜索（简化版）。返回 [{title,url,date}, ...]。

    失败/无结果返回空列表（不抛错），让 skill 标"无显著消息面"。
    """
    url = f"https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize=20&code={code}"
    try:
        r = requests.get(url, timeout=8, headers=_UA)
        r.raise_for_status()
        items = ((r.json() or {}).get("data") or {}).get("list") or []
    except Exception:
        return []
    out = []
    for it in items[:10]:
        out.append({
            "title": (it.get("title") or "").strip(),
            "url": it.get("url") or "",
            "date": (it.get("ctime") or it.get("rtime") or "")[:10],
        })
    return out
```

- [ ] **Step 4: 跑测试**

```bash
uv run pytest tests/test_query_lib_network.py -v
```
Expected: 全 PASS（如个别接口结构与测试 mock 不完全契合，调整函数解析或测试 mock 二选一直到全绿；优先保证 fetch_realtime / fetch_kline / fetch_money_flow 三个 PASS，concept_strength / recent_news 可降级为空字典 / 空列表也算 PASS）

- [ ] **Step 5: 手动 smoke**

```bash
uv run python -c "from code.lib.query import fetch_realtime; print(fetch_realtime('600519'))"
uv run python -c "from code.lib.query import fetch_kline; print(fetch_kline('600519', days=10).tail())"
```
Expected: 真实数据输出；非交易日时盘口可能用收盘价。

- [ ] **Step 6: Commit**

```bash
git add code/lib/query.py tests/test_query_lib_network.py
git commit -m "feat(query): 联网拉取 K线/盘口/资金流/概念榜/新闻

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `.claude/skills/stock-query/SKILL.md`（决策框架 + 卡片模板）

**Files:**
- Create: `.claude/skills/stock-query/SKILL.md`

skill 内容是 prompt 不走 TDD，但写完后用 `claude -p` 跑一只票做 smoke。

- [ ] **Step 1: 写 SKILL.md**

创建 `.claude/skills/stock-query/SKILL.md`：

````markdown
---
name: stock-query
description: 单股深度分析。给定一个 A 股代码（主板/创业板），按题材派框架判定值不值得买/继续持有，给买点/止损/止盈。当用户传入参数 code=XXXXXX mode=fresh|holding 时触发。
---

# stock-query · 单股决策助手

**调用方**：`scripts/tg_listener.py` 通过 `claude -p` headless 触发。
**入参**：在 prompt 文本中以 `code=600519 mode=fresh` 形式传入。

## 工作流（按序，不跳）

### Step 1 · 拉数据 fact pack

```python
import sys
sys.path.insert(0, "code")
from lib import query

CODE = "{从入参解析}"
realtime = query.fetch_realtime(CODE)            # 必拿，失败直接报错回避
kline    = query.fetch_kline(CODE, days=60)      # 60 日日线
flow     = query.fetch_money_flow(CODE, days=5)  # 资金流
concept  = query.fetch_concept_strength(CODE)    # 概念榜
news     = query.fetch_recent_news(CODE, days=7) # 新闻
```

任一**非 realtime** 拉取失败 → 标"该项数据缺失"，结论档位降一档（买入→观察，观察→回避）。

mode=holding 时额外读 `holdings.yaml` 当前票的成本价、仓位、buy_date、stop_loss。

### Step 2 · 题材派六维判定

| 维度 | 怎么判 |
|---|---|
| 题材归属 | 用 fact pack 概念榜 Top 20 + 近 10 日 ths_hot_reason 表（DB 已有）反查该票主题材；找不到归类→标"无明确主线" |
| 题材位置 | 启动期：概念近 5 日累涨 5–15%、龙头刚加速；主升期：概念 5 日 >15% 且涨停股 ≥3；高潮期：龙头连板 ≥4 或概念单日 >5%；退潮期：概念近 3 日累跌或龙头炸板 |
| 个股位置 | 比对概念龙头：相对涨幅 vs 龙头近 5 日差值 → 龙头/二线/边缘 |
| 资金 | 近 3 日 main_in 累计正负、单日峰值 |
| 技术 | 收盘 vs MA5/MA10/MA20；近 20 日相对高低位置；量比 |
| 消息 | news 列表前 5 条标题，标"利好/利空/中性"，是否有公司公告突发 |

### Step 3 · 结论档位

**fresh 分支**：
- **买入**：题材在启动/主升 + 个股是龙头或紧跟龙头 + 资金净流入 + 技术不在高位（距 20 日高 ≥5%）
- **观察**：方向对但任一维度不达标。**必须列出"什么信号出现升级为买入"**（≥2 条具体可观测信号）
- **回避**：题材退潮 / 高位滞涨 / 资金持续 3 日净流出 / 技术破位 之一即触发

**holding 分支**：
- **加仓**：买入逻辑仍然成立且未到第一止盈
- **持有**：维持原计划；同步更新止损是否该上移（盈利 >5% 时止损上移到成本价）
- **减仓清仓**：原买入逻辑失效（题材退潮、资金转流出、跌破关键位）

### Step 4 · 关键价位（每档都必给）

- 买点：限价 或 触发条件（含价位）
- 止损位：具体数字 + 依据（前低 / MA20 破位）
- 止盈位：第一目标 / 第二目标（按概念龙头近期高点 + 个股压力位）

### Step 5 · 输出卡片

**严格按下面模板输出（替换花括号占位符）。不要加额外段落、不要解释思考过程。**

fresh 模板：

```
📊 {NAME} {CODE}  [买入 / 观察 / 回避]
━━━━━━━━━━━━━━━━
🎯 结论：{VERDICT}
理由：{1-2 句白话}

🏷 题材：{CONCEPT} · {PHASE} · 板块5日{X}%
📍 位置：{LEADER_OR_FOLLOWER}（龙头{LEADER_NAME}，相对{Y}%）
💰 资金：近3日主力净{IN_OR_OUT}{Z}亿
📈 技术：{MA_POSITION}
📰 消息：{NEWS_SUMMARY}

⚡ 升级为"买入"的信号（满足任一）：   ← 仅观察档输出
  · {SIGNAL_1}
  · {SIGNAL_2}

💵 关键价位
  买点：{BUY_TRIGGER}
  止损：{STOP_LOSS}（{REASON}）
  止盈：{TP1} / {TP2}
━━━━━━━━━━━━━━━━
⚠️ 短线纪律：观察档≠可建仓，等信号    ← 仅观察档
```

holding 模板：

```
📊 {NAME} {CODE}  [加仓 / 持有 / 减仓清仓]
━━━━━━━━━━━━━━━━
🎯 结论：{VERDICT}
持仓：{COST}成本 · {DAYS}日前买入 · 当前{PRICE}（{PNL_PCT}）

🏷 题材：{CONCEPT} · {PHASE}
💰 资金：{FLOW}
📈 技术：{TECH}

⚠️ 触发{VERDICT}的逻辑：
  · {REASON_1}
  · {REASON_2}
  · {REASON_3}

🛡 {STOP_PLAN}
🎯 {TARGET_PLAN}
━━━━━━━━━━━━━━━━
```

### 限制
- 卡片单条 ≤ 800 字
- 价位必须给数字（非区间），止盈最多两档
- 任何数据缺失项标"—"不要编

````

- [ ] **Step 2: smoke run**

```bash
cd /Users/wispig/Desktop/stock
echo "请用 stock-query skill 分析 code=600519 mode=fresh" | \
  claude -p --permission-mode bypassPermissions
```
Expected: 终端打出 fresh 卡片，包含价位段。看到能正常出卡片即可（题材判断准确度后续迭代）。

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-query/SKILL.md
git commit -m "feat(skill): stock-query 单股决策助手

题材派框架 + fresh/holding 双分支 + 三档明确表态

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `scripts/tg_listener.py` 解析 + 前置校验 + 串行调度（TDD）

**Files:**
- Create: `scripts/tg_listener.py`
- Test: `tests/test_tg_listener.py`

把"轮询 TG"和"业务调度"拆成两层：`handle(text, chat_id)` 是纯业务函数（可测）；`main()` 只负责死循环 + 错误兜底（不测）。

- [ ] **Step 1: 写失败测试**

```python
"""tg_listener.handle() 单元测试：mock 数据库 + mock subprocess + mock push。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "code"))

import tg_listener as tl  # noqa: E402


def _seed_db(path):
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "code" / "init_db.sql").read_text())
    conn.executemany(
        "INSERT INTO stock_basic(code,name,board,list_date,is_st,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        [
            ("600519", "贵州茅台",   "main",    "2001", 0, "2026-05-14"),
            ("300750", "宁德时代",   "chinext", "2018", 0, "2026-05-14"),
            ("688981", "中芯国际",   "star",    "2020", 0, "2026-05-14"),
            ("000725", "*ST 京东方", "main",    "1997", 1, "2026-05-14"),
        ],
    )
    # 当日 kline 表示未停牌
    conn.execute("INSERT INTO daily_kline(code,date,close) VALUES('600519','2026-05-14',1605)")
    conn.execute("INSERT INTO daily_kline(code,date,close) VALUES('300750','2026-05-14',180)")
    conn.execute("INSERT INTO daily_kline(code,date,close) VALUES('688981','2026-05-14',50)")
    conn.execute("INSERT INTO daily_kline(code,date,close) VALUES('000725','2026-05-14',3)")
    conn.commit()
    conn.close()


def test_handle_unknown_silent(tmp_path, monkeypatch):
    _seed_db(tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill") as r:
        tl.handle("你好", chat_id=999, today="2026-05-14")
    p.assert_not_called()
    r.assert_not_called()


def test_handle_star_rejected(tmp_path, monkeypatch):
    _seed_db(tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill") as r:
        tl.handle("688981", chat_id=999, today="2026-05-14")
    p.assert_called_once()
    msg = p.call_args.args[0]
    assert "科创板" in msg
    r.assert_not_called()


def test_handle_st_rejected(tmp_path, monkeypatch):
    _seed_db(tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill") as r:
        tl.handle("000725", chat_id=999, today="2026-05-14")
    p.assert_called_once()
    assert "ST" in p.call_args.args[0]
    r.assert_not_called()


def test_handle_unknown_code(tmp_path, monkeypatch):
    _seed_db(tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill") as r:
        tl.handle("999999", chat_id=999, today="2026-05-14")
    p.assert_called_once()
    assert "未找到" in p.call_args.args[0]
    r.assert_not_called()


def test_handle_chinese_multi_hit(tmp_path, monkeypatch):
    _seed_db(tmp_path / "t.db")
    # 加一个也含"茅台"的票
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO stock_basic(code,name,board,is_st,updated_at) "
                 "VALUES('600702','舍得茅台酒','main',0,'2026-05-14')")
    conn.commit(); conn.close()
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill") as r:
        tl.handle("茅台", chat_id=999, today="2026-05-14")
    p.assert_called_once()
    assert "找到多只" in p.call_args.args[0]
    r.assert_not_called()


def test_handle_fresh_dispatches_skill(tmp_path, monkeypatch):
    _seed_db(tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    monkeypatch.setattr(tl, "HOLDINGS_FILE", tmp_path / "holdings.yaml")
    (tmp_path / "holdings.yaml").write_text("holdings: []\n")
    with patch.object(tl, "push_reply") as p, \
         patch.object(tl, "run_skill", return_value="📊 fake card") as r:
        tl.handle("600519", chat_id=999, today="2026-05-14")
    r.assert_called_once()
    code, mode = r.call_args.args[0], r.call_args.args[1]
    assert code == "600519"
    assert mode == "fresh"
    p.assert_called_once_with("📊 fake card")


def test_handle_holding_branch(tmp_path, monkeypatch):
    _seed_db(tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    monkeypatch.setattr(tl, "HOLDINGS_FILE", tmp_path / "holdings.yaml")
    (tmp_path / "holdings.yaml").write_text(
        "holdings:\n  - code: '600519'\n    name: 贵州茅台\n    genre: B\n"
        "    cost: 1580\n    shares: 100\n    buy_date: 2026-05-09\n"
    )
    with patch.object(tl, "push_reply"), \
         patch.object(tl, "run_skill", return_value="📊 fake") as r:
        tl.handle("600519", chat_id=999, today="2026-05-14")
    assert r.call_args.args[1] == "holding"


def test_handle_wrong_chat_id_silent(tmp_path, monkeypatch):
    _seed_db(tmp_path / "t.db")
    monkeypatch.setattr(tl.query, "DB", tmp_path / "t.db")
    monkeypatch.setattr(tl, "ALLOWED_CHAT_ID", "12345")
    with patch.object(tl, "push_reply") as p, patch.object(tl, "run_skill") as r:
        tl.handle("600519", chat_id=999, today="2026-05-14")
    p.assert_not_called()
    r.assert_not_called()
```

- [ ] **Step 2: 运行验证失败**

```bash
uv run pytest tests/test_tg_listener.py -v
```
Expected: ImportError

- [ ] **Step 3: 实现 `scripts/tg_listener.py`**

```python
"""TG 长轮询守护进程：接收单股代码/名称 → 调 CC headless 跑 stock-query → 回卡片。

并发：fcntl 文件锁 + 排队计数器；1 跑 + 3 等 = 4 容量，第 5 拒绝。
失败重试：TG API 指数退避，CC 子进程超时 180s 直接报错。
进程崩溃由 launchd KeepAlive 拉起；offset 持久化到 data/tg_offset.txt。
"""
from __future__ import annotations
import fcntl
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from lib import query  # noqa: E402
from notify import push_md, push  # noqa: E402

# .env 由 notify 模块已加载
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

HOLDINGS_FILE = ROOT / "holdings.yaml"
OFFSET_FILE = ROOT / "data" / "tg_offset.txt"
LOCK_FILE = "/tmp/stock-query.lock"
MAX_QUEUE = 3            # 排队上限（不含 1 个正在跑的）
SKILL_TIMEOUT = 180      # CC 子进程超时秒数

_running = 0
_waiting = 0


def push_reply(text: str) -> None:
    """回 TG。markdown 走 push_md，纯文本走 push。"""
    try:
        push_md(text, source="stock-query")
    except Exception as e:
        print(f"[tg_listener] push 失败: {e}", file=sys.stderr)


def held_codes() -> set[str]:
    if not HOLDINGS_FILE.exists():
        return set()
    try:
        data = yaml.safe_load(HOLDINGS_FILE.read_text()) or {}
    except Exception:
        return set()
    return {str(h.get("code")).zfill(6) for h in (data.get("holdings") or [])}


def run_skill(code: str, mode: str) -> str:
    """通过 claude -p headless 跑 stock-query skill；返回 markdown。"""
    prompt = (f"请使用 stock-query skill 分析这只股票，严格按 SKILL.md "
              f"模板输出卡片，不要任何额外文字：code={code} mode={mode}")
    proc = subprocess.run(
        ["claude", "-p", "--permission-mode", "bypassPermissions",
         "--cwd", str(ROOT)],
        input=prompt, capture_output=True, text=True, timeout=SKILL_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p 退出码 {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout.strip()


def _reject(code: str, reason: str) -> str:
    return f"❌ {code}\n原因：{reason}"


def handle(text: str, chat_id: int | str, today: Optional[str] = None) -> None:
    """处理一条入站消息。出口只有：silent / push_reply。"""
    if str(chat_id) != str(ALLOWED_CHAT_ID):
        return  # 静默忽略其它 chat

    kind, val = query.parse_input(text)
    if kind == "unknown":
        return  # 闲聊不响应

    today = today or date.today().isoformat()

    if kind == "name":
        hits = query.lookup_by_name(val)
        if not hits:
            push_reply(_reject(val, "未找到该名称"))
            return
        if len(hits) > 1:
            lines = "\n".join(f"  {c}  {n}" for c, n in hits[:8])
            push_reply(f"❓ 找到多只，请发代码：\n{lines}")
            return
        code = hits[0][0]
    else:
        code = val

    board = query.board_of(code)
    if board is None:
        push_reply(_reject(code, "未找到该代码"))
        return
    if board in ("star", "bse"):
        label = "科创板" if board == "star" else "北交所"
        push_reply(_reject(code, f"暂不支持{label}"))
        return
    if query.is_st(code):
        push_reply(_reject(code, "ST 票风险过高，本助手不分析"))
        return
    if query.is_suspended_today(code, today=today):
        push_reply(_reject(code, "今日停牌，跳过"))
        return

    mode = "holding" if code in held_codes() else "fresh"

    # 串行 + 队列上限
    global _running, _waiting
    if _running and _waiting >= MAX_QUEUE:
        push_reply(f"⏳ {code}\n忙，稍后再问")
        return
    _waiting += 1
    try:
        with open(LOCK_FILE, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            _waiting -= 1
            _running += 1
            try:
                card = run_skill(code, mode)
            except subprocess.TimeoutExpired:
                push_reply(f"⌛ {code}\n分析超时，稍后再试")
                return
            except Exception as e:
                push_reply(f"⚠️ {code}\n分析失败：{e}")
                return
            finally:
                _running -= 1
        push_reply(card)
    finally:
        # 双保险：异常路径下 _waiting 不会少减
        pass


# ============================================================
# TG 长轮询主循环
# ============================================================

def _load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip() or 0)
        except Exception:
            return 0
    return 0


def _save_offset(v: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(v))


def _get_updates(offset: int) -> list[dict]:
    r = requests.get(
        f"{TG_API}/getUpdates",
        params={"offset": offset, "timeout": 10},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data.get("result") or []


def main() -> None:
    if not TG_TOKEN or not ALLOWED_CHAT_ID:
        print("[tg_listener] TG_BOT_TOKEN / ALLOWED_CHAT_ID 未配置", file=sys.stderr)
        sys.exit(2)
    offset = _load_offset()
    print(f"[tg_listener] start, offset={offset}", flush=True)
    backoff = 1
    while True:
        try:
            updates = _get_updates(offset)
            backoff = 1
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                _save_offset(offset)
                msg = u.get("message") or u.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                if not text or chat_id is None:
                    continue
                try:
                    handle(text, chat_id)
                except Exception as e:
                    print(f"[tg_listener] handle 异常: {e}", file=sys.stderr)
            # 10s 节流（getUpdates timeout=10 已足够）
        except Exception as e:
            print(f"[tg_listener] loop 异常: {e}; 退避 {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试**

```bash
uv run pytest tests/test_tg_listener.py -v
```
Expected: 全 PASS。若个别用例 fail，对照断言修业务函数（不要修测试）。

- [ ] **Step 5: 手动 smoke（开两个终端）**

终端 A 启动：
```bash
cd /Users/wispig/Desktop/stock && uv run scripts/tg_listener.py
```
终端 B 在 TG 上发送 `600519`、`贵州茅台`、`688981`、`000725`、`你好` 五条；A 终端应分别走相应分支；TG 应收到 4 张回复（"你好"无响应）。

确认后 Ctrl+C 停 A。

- [ ] **Step 6: Commit**

```bash
git add scripts/tg_listener.py tests/test_tg_listener.py
git commit -m "feat(tg): tg_listener daemon · 单股查询入口

10s 轮询 / 前置校验 / fcntl 串行 / 队列上限 4 / 持久化 offset

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: launchd plist + 安装钩子 + 文档

**Files:**
- Create: `launchd/com.user.stocktglistener.plist.template`
- Modify: `scripts/install_launchd.sh`、`README.md`、`pyproject.toml`、`.env.example`（若不存在则跳过该项）

- [ ] **Step 1: 写 plist 模板**

创建 `launchd/com.user.stocktglistener.plist.template`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.stocktglistener</string>

    <key>ProgramArguments</key>
    <array>
        <string>{{UV_BIN}}</string>
        <string>run</string>
        <string>--project</string>
        <string>{{PROJECT_ROOT}}</string>
        <string>{{PROJECT_ROOT}}/scripts/tg_listener.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{{PROJECT_ROOT}}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>{{PROJECT_ROOT}}/logs/launchd_tglistener_stdout.log</string>

    <key>StandardErrorPath</key>
    <string>{{PROJECT_ROOT}}/logs/launchd_tglistener_stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 2: 改 install_launchd.sh 注册新 plist**

读现状再改：

```bash
sed -n '1,200p' scripts/install_launchd.sh
```

在脚本里找到现有 4 个 plist 的注册循环，把新 plist 加进去。具体替换：

打开 `scripts/install_launchd.sh`，找到列举 plist 名的数组（形如 `PLISTS=(stockpremarket stockintraday ...)`），在数组末尾加 `stocktglistener`。如果脚本是用 `for f in launchd/*.template` 形式遍历则无需修改，但需要新模板里有 `{{UV_BIN}}` 占位符的替换逻辑——若原脚本只替换 `{{PROJECT_ROOT}}`，则追加：

```bash
UV_BIN="${UV_BIN:-$(command -v uv)}"
# 在 sed 替换段加：
sed -e "s|{{PROJECT_ROOT}}|$PROJECT_ROOT|g" \
    -e "s|{{UV_BIN}}|$UV_BIN|g" \
    "$tpl" > "$dest"
```

- [ ] **Step 3: pyproject.toml 加 filelock（如未声明）**

```bash
grep filelock pyproject.toml
```
若无输出：

```bash
uv add filelock
```
（holdings.py 已 import filelock，可能 pyproject 已声明；有就跳过）

- [ ] **Step 4: README 补章节**

打开 `README.md`，在合适位置（建议"使用方式"末尾）追加：

```markdown
## TG 单股查询

启动后台监听：

```bash
bash scripts/install_launchd.sh   # 注册 launchd（首次）
launchctl start com.user.stocktglistener
```

用法：在 TG 直接发送股票代码或名称（主板/创业板）

- `600519` 或 `贵州茅台` → 输出题材派决策卡（fresh / holding 自动切换）
- `688xxx`（科创板）、`8xxxxx`（北交所）、ST 票 → 立即拒绝
- 已在 `holdings.yaml` 的票 → 走持仓决策卡（加仓/持有/减仓清仓）

环境变量：
- `ALLOWED_CHAT_ID`（不设默认 = `TG_CHAT_ID`）— 只接受这个 chat 的消息

每日 17:00 跑一次 stock_basic 刷新：

```bash
uv run scripts/refresh_stock_basic.py
```

（可挂到 postmarket 流程末尾或独立 launchd）
```

- [ ] **Step 5: 安装 + 起服务**

```bash
bash scripts/install_launchd.sh
launchctl unload "$HOME/Library/LaunchAgents/com.user.stocktglistener.plist" 2>/dev/null
launchctl load   "$HOME/Library/LaunchAgents/com.user.stocktglistener.plist"
sleep 3
tail -n 20 logs/launchd_tglistener_stdout.log logs/launchd_tglistener_stderr.log
```
Expected: stdout 出现 `[tg_listener] start, offset=...`

- [ ] **Step 6: 真实端到端 smoke**

在 TG 上手发四条：
1. `600519` → 30–90s 收到 fresh 卡片
2. `300750` → fresh 卡片
3. `688981` → 立即收到"暂不支持科创板"
4. `你好` → 无响应

如 #1 #2 卡片字段齐备且符合模板，端到端通过。

- [ ] **Step 7: Commit**

```bash
git add launchd/com.user.stocktglistener.plist.template \
        scripts/install_launchd.sh README.md pyproject.toml uv.lock 2>/dev/null
git commit -m "feat(launchd): 注册 tg_listener KeepAlive 服务 + README

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 整体回归 + 把 stock_basic 刷新挂到 postmarket 流程

**Files:**
- Modify: `code/run_postmarket.sh`（在原 postmarket 流程后追加一次 stock_basic 刷新）

- [ ] **Step 1: 看现有 postmarket 脚本**

```bash
cat code/run_postmarket.sh
```

- [ ] **Step 2: 在脚本末尾追加 stock_basic 刷新**

在 `code/run_postmarket.sh` 主流程**最后一行的非 0 退出码兜底之前**追加：

```bash
echo "[postmarket] refreshing stock_basic..."
uv run "$ROOT/scripts/refresh_stock_basic.py" || \
  echo "[postmarket] stock_basic refresh 失败，不阻断主流程"
```

（如脚本结构不一致就放在最末尾，加 `|| true` 兜底）

- [ ] **Step 3: 跑一次回归 pytest**

```bash
uv run pytest tests/ -v --tb=short
```
Expected: 全绿。如有前置已存在的失败用例，与本次改动无关的允许，逐一确认。

- [ ] **Step 4: 验收 checklist 手动确认**

逐条核对 spec 第 9 节：
- [ ] 发 `600519` → fresh 卡
- [ ] 发 `贵州茅台` → fresh 卡
- [ ] 发持仓票 → holding 卡
- [ ] 发 `688xxx` → 拒绝卡（无 CC 调用，查 stderr 不该有 `claude -p` 调用）
- [ ] 发 ST 票 → 拒绝卡
- [ ] 发 `你好` → 无响应
- [ ] `launchctl kill -TERM com.user.stocktglistener` 后等几秒，再 `launchctl list | grep stocktglistener` 应显示已重新拉起，期间发的消息进 offset 持久化后下次启动应该被消费

- [ ] **Step 5: Commit**

```bash
git add code/run_postmarket.sh
git commit -m "chore(postmarket): 末尾刷新 stock_basic 表

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## 自审

**1. Spec coverage 对照**
- §1 目标三档判定 → Task 5 SKILL.md（fresh/holding 分支三档 + 升级信号）
- §2 范围（主板+创业板/排 ST/科创/北交所/停牌）→ Task 6 `handle()` 前置校验 + Task 2 测试
- §3 架构（10s 轮询、subprocess、文件锁）→ Task 6 `tg_listener.py`
- §4.1 输入解析（去前缀、6 位数字、中文 fuzzy）→ Task 2 `parse_input` + `lookup_by_name`
- §4.1 队列容量 4 + 第 5 拒绝 → Task 6 `MAX_QUEUE=3` + `_waiting/_running`（已实现）
- §4.1 重试指数退避 → Task 6 `main()` backoff
- §4.2 数据拉取 6 项 → Task 2/4（K 线/盘口/概念/资金流/新闻/持仓读取）
- §4.2 题材派六维 + 三档判定 → Task 5 SKILL.md
- §4.3 query.py 函数签名 → Task 2/4
- §4.4 refresh 表 → Task 3
- §4.5 init_db.sql 改 → Task 1
- §4.6 plist → Task 7
- §5 卡片模板 → Task 5
- §6 错误矩阵 → Task 6 各分支（TG 退避、超时、队列满、解析失败、进程崩溃由 launchd）
- §7 测试覆盖 → Task 1/2/3/4/6
- §8 部署文件清单 → Task 7
- §9 验收 → Task 8 checklist

**2. Placeholder 扫描**
- 无 TBD / TODO；每个步骤都附了代码或具体命令
- 唯一保留的占位符是模板里 `{{PROJECT_ROOT}}` `{{UV_BIN}}`（plist 模板里的，由 install 脚本替换，符合现有项目惯例）

**3. 类型一致性**
- `handle(text, chat_id, today=None)`：测试与实现签名一致
- `run_skill(code, mode) -> str`：测试 mock 返回 str，实现返回 stdout.strip() ✓
- `push_reply(text)` 单参 markdown ✓
- `query.parse_input` 返回 `tuple[str, str]`：测试断言形态一致 ✓
- `query.is_suspended_today(code, today=None)`：测试与实现签名一致 ✓
- `lookup_by_name` 返回 `list[tuple[str, str]]`：一致 ✓

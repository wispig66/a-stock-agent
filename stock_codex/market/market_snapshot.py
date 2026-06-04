"""盘中紧凑市场快照。"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from stock_codex.infra.db import connect_close
from stock_codex.market.theme_graph import ThemeGraph
from stock_codex.paths import DB_FILE


SNAPSHOT_INTERVAL_SECONDS = 5 * 60
STALE_AFTER_SECONDS = 10 * 60

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts TEXT NOT NULL UNIQUE,
    trade_date TEXT NOT NULL,
    is_stale INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_market_snapshot_date_ts
    ON market_snapshot(trade_date, snapshot_ts);
"""


def ensure_schema(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_close(path) as conn:
        conn.executescript(SCHEMA_SQL)


def _number(value, default: float = 0.0) -> float:
    try:
        num = float(value)
        return num if math.isfinite(num) else default
    except (TypeError, ValueError):
        return default


def _code(value) -> str:
    raw = str(value or "").strip()
    match = re.search(r"(\d{6})$", raw)
    return match.group(1) if match else ""


def _default_a_spot_fetcher() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_spot()


def _default_index_fetcher() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_index_spot_sina()


def _default_concept_flow_fetcher() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_fund_flow_concept(symbol="即时")


def _default_news_fetcher():
    import akshare as ak
    return ak.stock_info_global_cls(symbol="全部")


class MarketSnapshot:
    """每 5 分钟采集一次市场事实；其他 tick 复用最近快照。"""

    def __init__(
        self,
        db_path: str | Path = DB_FILE,
        graph: ThemeGraph | None = None,
        *,
        a_spot_fetcher: Callable = _default_a_spot_fetcher,
        index_fetcher: Callable = _default_index_fetcher,
        concept_flow_fetcher: Callable = _default_concept_flow_fetcher,
        overseas_fetcher: Callable | None = None,
        news_fetcher: Callable = _default_news_fetcher,
    ):
        self.db_path = Path(db_path)
        self.graph = graph or ThemeGraph(db_path=self.db_path)
        self.a_spot_fetcher = a_spot_fetcher
        self.index_fetcher = index_fetcher
        self.concept_flow_fetcher = concept_flow_fetcher
        self.overseas_fetcher = overseas_fetcher or self._fetch_overseas_sina
        self.news_fetcher = news_fetcher
        ensure_schema(self.db_path)

    def latest(self, trade_date: str | None = None) -> dict | None:
        where = "WHERE trade_date=?" if trade_date else ""
        params = (trade_date,) if trade_date else ()
        with connect_close(self.db_path) as conn:
            row = conn.execute(
                f"SELECT payload_json FROM market_snapshot {where} ORDER BY snapshot_ts DESC LIMIT 1",
                params,
            ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _age_seconds(now: datetime, snapshot_at: str | None) -> int | None:
        if not snapshot_at:
            return None
        try:
            return max(0, int((now - datetime.fromisoformat(snapshot_at)).total_seconds()))
        except ValueError:
            return None

    def _source(
        self,
        name: str,
        now: datetime,
        fetcher: Callable,
        normalize: Callable,
        latest: dict | None,
        cache_key: str,
    ) -> tuple[object, dict]:
        try:
            value = normalize(fetcher())
            return value, {
                "source": "live",
                "snapshot_at": now.isoformat(timespec="seconds"),
                "age_seconds": 0,
            }
        except Exception as exc:
            cached = deepcopy((latest or {}).get(cache_key))
            previous = ((latest or {}).get("source_status") or {}).get(name) or {}
            snapshot_at = previous.get("snapshot_at")
            return cached if cached is not None else normalize(None), {
                "source": "cache" if cached is not None else "missing",
                "snapshot_at": snapshot_at,
                "age_seconds": self._age_seconds(now, snapshot_at),
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _expired_cache(status: dict) -> bool:
        age = status.get("age_seconds")
        return status.get("source") == "cache" and (
            age is None or int(age) > STALE_AFTER_SECONDS
        )

    def _refresh_reused_snapshot(self, latest: dict, now: datetime) -> dict:
        reused = deepcopy(latest)
        reused["is_new"] = False
        statuses = reused.get("source_status") or {}
        for status in statuses.values():
            status["age_seconds"] = self._age_seconds(now, status.get("snapshot_at"))
        if self._expired_cache(statuses.get("indices") or {}):
            reused["indices"] = {}
        if self._expired_cache(statuses.get("overseas") or {}):
            reused["overseas"] = {}
        if self._expired_cache(statuses.get("news") or {}):
            reused["news"] = []
        reused["is_stale"] = (
            not reused.get("stocks")
            or not reused.get("concept_flow")
            or any(
                status.get("age_seconds") is None
                or int(status["age_seconds"]) > STALE_AFTER_SECONDS
                for status in (
                    statuses.get("a_spot") or {},
                    statuses.get("concept_flow") or {},
                )
            )
        )
        return reused

    @staticmethod
    def _normalize_stocks(value) -> dict[str, dict]:
        if not isinstance(value, pd.DataFrame) or value.empty:
            return {}
        if not ({"最新价", "price"} & set(value.columns)) or not (
            {"涨跌幅", "pct"} & set(value.columns)
        ):
            raise ValueError("A 股行情缺少最新价或涨跌幅字段")
        out: dict[str, dict] = {}
        for _, row in value.iterrows():
            code = _code(row.get("代码") or row.get("code"))
            if not code:
                continue
            out[code] = {
                "name": str(row.get("名称") or row.get("name") or ""),
                "price": _number(row.get("最新价") if "最新价" in row else row.get("price")),
                "pct": _number(row.get("涨跌幅") if "涨跌幅" in row else row.get("pct")),
                "amount": _number(row.get("成交额") if "成交额" in row else row.get("amount")),
            }
        return out

    @staticmethod
    def _normalize_indices(value) -> dict[str, dict]:
        if not isinstance(value, pd.DataFrame) or value.empty:
            return {}
        wanted = {"上证指数", "深证成指", "创业板指", "科创50"}
        out: dict[str, dict] = {}
        for _, row in value.iterrows():
            name = str(row.get("名称") or row.get("name") or "")
            if name not in wanted:
                continue
            out[name] = {
                "pct": _number(row.get("涨跌幅") if "涨跌幅" in row else row.get("pct")),
                "amount": _number(row.get("成交额") if "成交额" in row else row.get("amount")),
            }
        return out

    @staticmethod
    def _normalize_concept_flow(value) -> list[dict]:
        if not isinstance(value, pd.DataFrame) or value.empty:
            return []
        if not ({"行业-涨跌幅", "涨跌幅", "pct"} & set(value.columns)):
            raise ValueError("概念资金流缺少涨跌幅字段")
        out: list[dict] = []
        for _, row in value.iterrows():
            name = str(
                row.get("行业")
                or row.get("概念名称")
                or row.get("板块名称")
                or row.get("name")
                or ""
            )
            if not name:
                continue
            out.append({
                "name": name,
                "pct": _number(
                    row.get("行业-涨跌幅")
                    if "行业-涨跌幅" in row
                    else row.get("涨跌幅")
                    if "涨跌幅" in row
                    else row.get("pct")
                ),
                "net_flow": _number(row.get("净额") if "净额" in row else row.get("净流入")),
                "company_count": int(_number(row.get("公司家数"))),
                "leader": str(row.get("领涨股") or row.get("领涨股票") or ""),
                "leader_pct": _number(
                    row.get("领涨股-涨跌幅")
                    if "领涨股-涨跌幅" in row
                    else row.get("领涨股票-涨跌幅")
                ),
            })
        return out[:100]

    def _normalize_news(self, value, as_of: datetime) -> list[dict]:
        if isinstance(value, pd.DataFrame):
            rows = value.to_dict("records")
        elif isinstance(value, list):
            rows = value
        else:
            rows = []
        out: list[dict] = []
        for row in rows[:50]:
            title = str(row.get("title") or row.get("标题") or row.get("内容") or "").strip()
            if not title:
                continue
            matches = self.graph.resolve("", "", "", title, as_of)
            out.append({
                "title": title[:200],
                "time": str(row.get("time") or row.get("发布时间") or ""),
                "themes": [match.theme for match in matches if not match.temporary],
            })
        return out

    @staticmethod
    def _normalize_overseas(value) -> dict[str, dict]:
        if not isinstance(value, dict):
            return {}
        out: dict[str, dict] = {}
        for symbol, row in value.items():
            if not isinstance(row, dict):
                continue
            out[str(symbol).upper()] = {
                "price": _number(row.get("price")),
                "pct": _number(row.get("pct")),
                "themes": [str(x) for x in (row.get("themes") or [])],
            }
        return out

    def _fetch_overseas_sina(self) -> dict[str, dict]:
        import requests
        symbols = self.graph.external_symbols()
        if not symbols:
            return {}
        query = ",".join(f"gb_{symbol.lower()}" for symbol in symbols)
        response = requests.get(
            f"https://hq.sinajs.cn/list={query}",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=6,
        )
        response.encoding = "gbk"
        out: dict[str, dict] = {}
        for line in response.text.splitlines():
            if "=\"" not in line:
                continue
            head, _, rest = line.partition("=\"")
            symbol = head.rsplit("_", 1)[-1].upper()
            fields = rest.strip("\";").split(",")
            if len(fields) < 4:
                continue
            out[symbol] = {
                "price": _number(fields[1]),
                "pct": _number(fields[3]),
                "themes": [match.theme for match in self.graph.resolve_external(symbol)],
            }
        return out

    @staticmethod
    def _breadth(stocks: dict[str, dict]) -> dict:
        values = [row["pct"] for row in stocks.values()]
        if not values:
            return {}
        up = sum(1 for pct in values if pct > 0)
        down = sum(1 for pct in values if pct < 0)
        flat = len(values) - up - down
        return {
            "total": len(values),
            "up": up,
            "down": down,
            "flat": flat,
            "up_ratio": round(up / len(values), 4),
            "down_ratio": round(down / len(values), 4),
        }

    @staticmethod
    def _is_limit_up_like(code: str, pct: float) -> bool:
        threshold = 19.5 if code.startswith(("30", "68")) else 9.8
        return pct >= threshold

    def _theme_strength(
        self,
        now: datetime,
        stocks: dict[str, dict],
        concept_flow: list[dict],
    ) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for theme in self.graph.themes:
            rows = [
                stocks[item["code"]]
                for item in self.graph.member_records(theme)
                if item["code"] in stocks
            ]
            if not rows:
                continue
            pcts = [row["pct"] for row in rows]
            out[theme] = {
                "pct": round(sum(pcts) / len(pcts), 2),
                "net_flow": 0.0,
                "company_count": len(rows),
                "leader": max(rows, key=lambda row: row["pct"])["name"],
                "leader_pct": max(pcts),
                "member_count": len(rows),
                "up_count": sum(1 for pct in pcts if pct > 0),
                "strong_count": sum(1 for pct in pcts if pct >= 3),
                "avg_pct": round(sum(pcts) / len(pcts), 2),
                "mapped_concepts": [],
            }

        for concept in concept_flow:
            matches = self.graph.resolve("", "", concept["name"], concept["name"], now)
            for match in matches:
                current = out.setdefault(match.theme, {
                    "pct": 0.0,
                    "net_flow": 0.0,
                    "company_count": 0,
                    "leader": "",
                    "leader_pct": 0.0,
                    "member_count": 0,
                    "up_count": 0,
                    "strong_count": 0,
                    "avg_pct": 0.0,
                    "mapped_concepts": [],
                    "temporary": match.temporary,
                    "candidate_allowed": match.candidate_allowed,
                })
                if concept["pct"] >= current["pct"]:
                    current["pct"] = concept["pct"]
                    current["leader"] = concept["leader"]
                    current["leader_pct"] = concept["leader_pct"]
                current["net_flow"] = round(current["net_flow"] + concept["net_flow"], 2)
                current["company_count"] = max(current["company_count"], concept["company_count"])
                if concept["name"] not in current["mapped_concepts"]:
                    current["mapped_concepts"].append(concept["name"])
        return out

    def _anchors(self, stocks: dict[str, dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for theme in self.graph.themes:
            for member in self.graph.member_records(theme):
                if member["role"] not in {"anchor", "anchors"} or member["code"] not in stocks:
                    continue
                stock = stocks[member["code"]]
                record = out.setdefault(member["code"], {
                    "name": stock["name"],
                    "price": stock["price"],
                    "pct": stock["pct"],
                    "amount": stock["amount"],
                    "themes": [],
                })
                current = theme
                while current:
                    if current not in record["themes"]:
                        record["themes"].append(current)
                    current = self.graph.parent_of(current)
        return out

    def capture(self, now: datetime) -> dict:
        trade_date = now.strftime("%Y-%m-%d")
        latest = self.latest(trade_date)
        if latest:
            age = self._age_seconds(now, latest.get("snapshot_ts"))
            if age is not None and age < SNAPSHOT_INTERVAL_SECONDS:
                return self._refresh_reused_snapshot(latest, now)

        stocks, a_status = self._source(
            "a_spot", now, self.a_spot_fetcher, self._normalize_stocks, latest, "stocks"
        )
        indices, index_status = self._source(
            "indices", now, self.index_fetcher, self._normalize_indices, latest, "indices"
        )
        concept_flow, flow_status = self._source(
            "concept_flow",
            now,
            self.concept_flow_fetcher,
            self._normalize_concept_flow,
            latest,
            "concept_flow",
        )
        overseas, overseas_status = self._source(
            "overseas",
            now,
            self.overseas_fetcher,
            self._normalize_overseas,
            latest,
            "overseas",
        )
        news, news_status = self._source(
            "news",
            now,
            self.news_fetcher,
            lambda value: self._normalize_news(value, now),
            latest,
            "news",
        )

        statuses = {
            "a_spot": a_status,
            "indices": index_status,
            "concept_flow": flow_status,
            "overseas": overseas_status,
            "news": news_status,
        }
        if self._expired_cache(index_status):
            indices = {}
        if self._expired_cache(overseas_status):
            overseas = {}
        if self._expired_cache(news_status):
            news = []
        critical_stale = not stocks or not concept_flow or any(
            status.get("age_seconds") is None or status["age_seconds"] > STALE_AFTER_SECONDS
            for status in (a_status, flow_status)
        )
        theme_strength = self._theme_strength(now, stocks, concept_flow)
        limit_up_like = sum(
            1 for code, row in stocks.items() if self._is_limit_up_like(code, row["pct"])
        )
        snapshot = {
            "snapshot_ts": now.isoformat(timespec="seconds"),
            "trade_date": trade_date,
            "is_new": True,
            "is_stale": critical_stale,
            "source_status": statuses,
            "breadth": self._breadth(stocks),
            "indices": indices,
            "turnover": {"amount": round(sum(row["amount"] for row in stocks.values()), 2)},
            "theme_strength": theme_strength,
            "overseas": overseas,
            "anchors": self._anchors(stocks),
            "pool_summary": {"limit_up_like": limit_up_like},
            "news": news,
            "stocks": stocks,
            "concept_flow": concept_flow,
        }
        with connect_close(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO market_snapshot
                   (snapshot_ts, trade_date, is_stale, payload_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    snapshot["snapshot_ts"],
                    trade_date,
                    int(snapshot["is_stale"]),
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
        return snapshot

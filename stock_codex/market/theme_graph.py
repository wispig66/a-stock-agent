"""层级题材目录与多证据归因。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path

import yaml

from stock_codex.infra.db import connect_close
from stock_codex.paths import DATA_DIR, DB_FILE


REASON_SPLIT = re.compile(r"[/+、,，]+")
SAME_DAY_REASON_AVAILABLE_AT = time(15, 30)
DEFAULT_CATALOG_PATH = DATA_DIR / "concept_whitelist.yaml"
PACKAGED_CATALOG_PATH = Path(__file__).with_name("default_concept_whitelist.yaml")


@dataclass(frozen=True)
class ThemeMatch:
    theme: str
    parent: str | None
    confidence: float
    source: str
    evidence: str = ""
    is_primary: bool = False
    candidate_allowed: bool = True
    temporary: bool = False
    member_role: str | None = None

    @property
    def tag(self) -> str:
        return self.theme


@dataclass(frozen=True)
class ThemeMember:
    code: str
    role: str


class ThemeGraph:
    """从 YAML 目录解析题材层级，并用成员、异动文本和近期 reason 做多标签归因。"""

    def __init__(
        self,
        catalog_path: str | Path = DEFAULT_CATALOG_PATH,
        *,
        db_path: str | Path = DB_FILE,
        fallback_path: str | Path | None = None,
    ):
        self.catalog_path = Path(catalog_path)
        if fallback_path is None and self.catalog_path == DEFAULT_CATALOG_PATH:
            fallback_path = PACKAGED_CATALOG_PATH
        self.fallback_path = Path(fallback_path) if fallback_path is not None else None
        self.db_path = Path(db_path)
        self.themes: dict[str, dict] = self._load_catalog()
        self._members_by_theme: dict[str, list[ThemeMember]] = {}
        self._themes_by_code: dict[str, list[tuple[str, str]]] = {}
        self._external_by_symbol: dict[str, list[tuple[str, str]]] = {}
        self._phrases_by_theme: dict[str, list[str]] = {}
        self._build_indexes()

    def __len__(self) -> int:
        return len(self.themes)

    def _load_catalog(self) -> dict[str, dict]:
        path = self.catalog_path
        if not path.exists() and self.fallback_path is not None:
            path = self.fallback_path
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return {str(theme): (conf or {}) for theme, conf in raw.items()}

    def _build_indexes(self) -> None:
        for theme, conf in self.themes.items():
            members = self._parse_members(conf.get("members"))
            self._members_by_theme[theme] = members
            for member in members:
                self._themes_by_code.setdefault(member.code, []).append((theme, member.role))

            external = self._parse_role_values(conf.get("external_symbols"), default_role="mapping")
            for symbol, role in external:
                self._external_by_symbol.setdefault(symbol.upper(), []).append((theme, role))

            phrases = [theme]
            phrases.extend(str(x).strip() for x in (conf.get("aliases") or []) if str(x).strip())
            phrases.extend(str(x).strip() for x in (conf.get("keywords") or []) if str(x).strip())
            self._phrases_by_theme[theme] = list(dict.fromkeys(phrases))

    @staticmethod
    def _parse_role_values(value, *, default_role: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if isinstance(value, dict):
            for key, items in value.items():
                if isinstance(items, (list, tuple, set)):
                    for item in items:
                        out.append((str(item).strip(), str(key)))
                else:
                    out.append((str(key).strip(), str(items or default_role)))
            return out
        for item in value or []:
            if isinstance(item, dict):
                raw = item.get("code") or item.get("symbol")
                if raw:
                    out.append((str(raw).strip(), str(item.get("role") or default_role)))
            else:
                out.append((str(item).strip(), default_role))
        return out

    def _parse_members(self, value) -> list[ThemeMember]:
        parsed = self._parse_role_values(value, default_role="member")
        return [
            ThemeMember(code=raw.zfill(6), role=role)
            for raw, role in parsed
            if raw
        ]

    def parent_of(self, theme: str) -> str | None:
        parent = self.themes.get(theme, {}).get("parent")
        return str(parent) if parent else None

    def aliases_for(self, theme: str) -> list[str]:
        return list(self._phrases_by_theme.get(theme, []))

    def external_symbols(self) -> list[str]:
        return sorted(self._external_by_symbol)

    def member_records(self, theme: str, *, include_children: bool = False) -> list[dict]:
        themes = [theme]
        if include_children:
            themes.extend(name for name in self.themes if self._is_descendant(name, theme))
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for name in themes:
            for member in self._members_by_theme.get(name, []):
                key = (member.code, name)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"code": member.code, "role": member.role, "theme": name})
        return out

    def _is_descendant(self, theme: str, parent: str) -> bool:
        current = self.parent_of(theme)
        while current:
            if current == parent:
                return True
            current = self.parent_of(current)
        return False

    @staticmethod
    def _contains(text: str, phrase: str) -> bool:
        if not text or not phrase:
            return False
        if phrase.isascii():
            pattern = rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])"
            return re.search(pattern, text, flags=re.IGNORECASE) is not None
        return phrase in text

    def _match_text(self, text: str) -> list[tuple[str, str]]:
        hits: list[tuple[str, str]] = []
        for theme, phrases in self._phrases_by_theme.items():
            for phrase in sorted(phrases, key=len, reverse=True):
                if self._contains(text, phrase):
                    hits.append((theme, phrase))
                    break
        return hits

    def _add_match(
        self,
        matches: dict[str, ThemeMatch],
        theme: str,
        confidence: float,
        source: str,
        evidence: str,
        *,
        member_role: str | None = None,
    ) -> None:
        if theme not in self.themes:
            return
        match = ThemeMatch(
            theme=theme,
            parent=self.parent_of(theme),
            confidence=confidence,
            source=source,
            evidence=evidence,
            member_role=member_role,
        )
        current = matches.get(theme)
        if current is None or match.confidence > current.confidence:
            matches[theme] = match

    def _reason_rows(self, code: str, as_of_date: str) -> list[tuple[str, str]]:
        if not code or not self.db_path.exists():
            return []
        try:
            with connect_close(self.db_path) as conn:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ths_hot_reason'"
                ).fetchone()
                if not has_table:
                    return []
                return conn.execute(
                    """SELECT date, reason FROM ths_hot_reason
                       WHERE code=? AND date<=? AND COALESCE(reason, '') != ''
                       ORDER BY date DESC""",
                    (code, as_of_date),
                ).fetchall()
        except sqlite3.Error:
            return []

    def _recent_scored_reason_dates(self, as_of_date: str) -> list[str]:
        if not self.db_path.exists():
            return []
        try:
            with connect_close(self.db_path) as conn:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ths_hot_reason'"
                ).fetchone()
                if not has_table:
                    return []
                return [
                    str(row[0])
                    for row in conn.execute(
                        """SELECT DISTINCT date FROM ths_hot_reason
                           WHERE date<=? ORDER BY date DESC LIMIT 4""",
                        (as_of_date,),
                    ).fetchall()
                ]
        except sqlite3.Error:
            return []

    @staticmethod
    def _as_of_parts(as_of: date | datetime | str) -> tuple[str, bool]:
        if isinstance(as_of, datetime):
            return as_of.date().isoformat(), as_of.time() >= SAME_DAY_REASON_AVAILABLE_AT
        if isinstance(as_of, date):
            return as_of.isoformat(), True
        parsed = datetime.fromisoformat(str(as_of))
        include_same_day = "T" not in str(as_of) and " " not in str(as_of)
        if not include_same_day:
            include_same_day = parsed.time() >= SAME_DAY_REASON_AVAILABLE_AT
        return parsed.date().isoformat(), include_same_day

    def _add_reason_matches(
        self,
        matches: dict[str, ThemeMatch],
        code: str,
        as_of: date | datetime | str,
    ) -> None:
        as_of_date, include_same_day = self._as_of_parts(as_of)
        rows = self._reason_rows(code, as_of_date)
        by_date: dict[str, list[str]] = {}
        for reason_date, reason in rows:
            by_date.setdefault(str(reason_date), []).append(str(reason))

        available_dates = self._recent_scored_reason_dates(as_of_date)
        prior_dates = [d for d in available_dates if d < as_of_date][:3]
        scored_dates: list[tuple[str, float, str]] = []
        if include_same_day and as_of_date in by_date:
            scored_dates.append((as_of_date, 0.90, "ths_reason_d0"))
        for idx, reason_date in enumerate(prior_dates):
            if idx == 0:
                scored_dates.append((reason_date, 0.75, "ths_reason_d1"))
            else:
                scored_dates.append((reason_date, 0.55, "ths_reason_d2_d3"))

        for reason_date, confidence, source in scored_dates:
            for reason in by_date.get(reason_date, []):
                for piece in (x for x in REASON_SPLIT.split(reason) if x):
                    for theme, phrase in self._match_text(piece):
                        self._add_match(matches, theme, confidence, source, f"{reason_date}:{phrase}")

    def _finalize(self, matches: dict[str, ThemeMatch]) -> list[ThemeMatch]:
        for child in list(matches.values()):
            parent = child.parent
            while parent:
                inherited = max(0.0, round(child.confidence - 0.05, 2))
                self._add_match(
                    matches,
                    parent,
                    inherited,
                    f"parent:{child.source}",
                    child.theme,
                )
                parent = self.parent_of(parent)

        if not matches:
            return []
        primary_theme = max(
            matches,
            key=lambda theme: (
                matches[theme].confidence,
                self._depth(theme),
                len(theme),
            ),
        )
        out = [
            replace(match, is_primary=(theme == primary_theme))
            for theme, match in matches.items()
        ]
        return sorted(out, key=lambda m: (not m.is_primary, -m.confidence, -self._depth(m.theme), m.theme))

    def _depth(self, theme: str) -> int:
        depth = 0
        current = self.parent_of(theme)
        while current:
            depth += 1
            current = self.parent_of(current)
        return depth

    def temporary_theme(self, name: str, *, source: str = "temporary_concept") -> ThemeMatch:
        return ThemeMatch(
            theme=name.strip(),
            parent=None,
            confidence=0.50,
            source=source,
            evidence=name.strip(),
            is_primary=True,
            candidate_allowed=False,
            temporary=True,
        )

    def resolve(
        self,
        code: str,
        name: str,
        sector_hint: str,
        info: str,
        as_of: date | datetime | str,
    ) -> list[ThemeMatch]:
        """返回多标签归因；主标签由最高置信度和题材层级共同决定。"""
        normalized_code = str(code or "").strip().zfill(6)
        matches: dict[str, ThemeMatch] = {}

        for theme, role in self._themes_by_code.get(normalized_code, []):
            self._add_match(matches, theme, 0.95, "catalog_member", normalized_code, member_role=role)

        anomaly_text = " ".join(x for x in (str(sector_hint or ""), str(info or "")) if x)
        for theme, phrase in self._match_text(anomaly_text):
            self._add_match(matches, theme, 0.85, "anomaly_keyword", phrase)

        for theme, phrase in self._match_text(str(name or "")):
            self._add_match(matches, theme, 0.60, "name_keyword", phrase)

        if normalized_code != "000000":
            self._add_reason_matches(matches, normalized_code, as_of)

        resolved = self._finalize(matches)
        if resolved:
            return resolved
        if str(sector_hint or "").strip():
            return [self.temporary_theme(str(sector_hint), source="temporary_sector_hint")]
        return []

    def resolve_external(self, symbol: str) -> list[ThemeMatch]:
        matches: dict[str, ThemeMatch] = {}
        normalized = str(symbol or "").strip().upper()
        for theme, role in self._external_by_symbol.get(normalized, []):
            self._add_match(matches, theme, 0.85, "external_symbol", normalized, member_role=role)
        return self._finalize(matches)

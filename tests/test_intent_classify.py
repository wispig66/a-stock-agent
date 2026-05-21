"""意图分类器：规则覆盖 + 显式覆盖 + LLM 桩 + 模糊兜底。"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

from stock_codex.market import intent  # noqa: E402


# 板块名词库：测试时注入；生产由 build_sector_lexicon() 从 DB 拉
LEX = {"光伏概念", "光伏", "人工智能", "AI", "储能", "新能源", "固态电池"}


# ────────── 显式覆盖 ──────────

def test_explicit_sector():
    r = intent.classify("sector=光伏", lexicon=LEX)
    assert r["intent"] == "sector"
    assert r["extracted"] == "光伏"
    assert r["source"] == "explicit"

def test_explicit_stock():
    r = intent.classify("stock=600519", lexicon=LEX)
    assert r["intent"] == "stock"
    assert r["extracted"] == "600519"

def test_explicit_event():
    r = intent.classify("event=国常会批了储能补贴", lexicon=LEX)
    assert r["intent"] == "event"
    assert "储能补贴" in r["extracted"]

def test_explicit_sector_empty_value():
    r = intent.classify("sector=", lexicon=LEX)
    assert r["intent"] == "error"
    assert "sector=" in r["extracted"]


# ────────── 规则匹配 ──────────

def test_rule_pure_code():
    r = intent.classify("600519", lexicon=LEX)
    assert r["intent"] == "stock"
    assert r["extracted"] == "600519"
    assert r["source"] == "rule"

def test_rule_code_with_name():
    r = intent.classify("600519 贵州茅台", lexicon=LEX)
    assert r["intent"] == "stock"
    assert r["extracted"] == "600519"

def test_rule_sector_in_lexicon():
    r = intent.classify("光伏怎么样", lexicon=LEX)
    assert r["intent"] == "sector"
    assert r["extracted"] == "光伏"

def test_rule_sector_substring():
    r = intent.classify("AI 还能不能上车", lexicon=LEX)
    assert r["intent"] == "sector"
    assert r["extracted"] == "AI"

def test_rule_event_keyword():
    r = intent.classify("国常会刚批了储能补贴", lexicon=LEX)
    assert r["intent"] == "event"
    assert "储能" in r["extracted"]

def test_rule_event_keyword_2():
    r = intent.classify("小米刚发布新车", lexicon=LEX)
    assert r["intent"] == "event"


# ────────── LLM 兜底 ──────────

def test_llm_returns_sector():
    fake_llm = lambda txt: {"intent": "sector", "extracted": "光伏", "confidence": 0.85}
    r = intent.classify("最近哪个新能源细分有戏", lexicon=set(), llm_call=fake_llm)
    assert r["intent"] == "sector"
    assert r["source"] == "llm"
    assert r["confidence"] == 0.85


def test_llm_low_confidence_falls_to_ambiguous():
    fake_llm = lambda txt: {"intent": "sector", "extracted": "光伏", "confidence": 0.3}
    r = intent.classify("xxx", lexicon=set(), llm_call=fake_llm)
    assert r["intent"] == "ambiguous"
    assert r["source"] == "ambiguous"
    # candidates 给前端展示
    assert isinstance(r["extracted"], dict)
    assert "candidates" in r["extracted"]


def test_llm_call_failure_falls_to_ambiguous():
    def boom(txt):
        raise RuntimeError("LLM 502")
    r = intent.classify("xxx", lexicon=set(), llm_call=boom)
    assert r["intent"] == "ambiguous"

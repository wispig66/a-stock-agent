from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SCHEDULED_SKILLS_REQUIRED_TERMS = {
    "stock-premarket": [
        "fact pack",
        "allowed facts",
        "data/last_card.md",
        "push.py",
        "非交易日",
        "数据源失败",
        "解禁检查失败",
        "IM 推送失败",
    ],
    "stock-intraday": [
        "当前系统时间",
        "09:30、09:45、11:30 或 14:30",
        "data/last_intraday_card.md",
        "--source stock-intraday",
        "PREMARKET_MISSING",
        "不要编造观察池",
    ],
    "stock-postmarket": [
        "data/last_postmarket_card.md",
        "--source stock-postmarket",
        "stock_basic",
        "scripts/refresh_stock_basic.py",
        "失败只报告为副作用失败",
        "不掩盖盘后主流程结果",
        "推送完成后刷新",
    ],
    "stock-weekly": [
        "data/weekly_review/YYYY-WW.md",
        "未 force",
        "跳过并报告",
        "不重复推送",
        "不跑 aggregate",
        "不做 WebSearch",
        "data/weekly_review",
        "data/last_weekly_card.md",
        "--source stock-weekly",
    ],
}


def skill_text(skill: str) -> str:
    return (ROOT / ".agents" / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def test_scheduled_skills_define_required_outputs_and_failures():
    for skill, required_terms in SCHEDULED_SKILLS_REQUIRED_TERMS.items():
        text = skill_text(skill)
        assert "Codex automation" in text
        assert "无人值守" in text
        assert "不要只回复完成" in text
        assert "Codex automation 最终回复只给简要运行摘要" in text
        assert "失败" in text
        for term in required_terms:
            assert term in text


def test_scheduled_skills_do_not_tell_codex_to_call_legacy_wrapper():
    for skill in SCHEDULED_SKILLS_REQUIRED_TERMS:
        text = skill_text(skill)
        assert ("cl" + "aude -p") not in text
        assert "run_premarket.sh" not in text
        assert "run_intraday.sh" not in text
        assert "run_postmarket.sh" not in text


def test_scheduled_skills_separate_cards_from_automation_summary():
    for skill in SCHEDULED_SKILLS_REQUIRED_TERMS:
        text = skill_text(skill)
        assert "唯一最终 assistant 消息" not in text
        assert "最终 assistant 消息必须是卡片" not in text
        assert "写入文件" in text
        assert "IM 推送" in text

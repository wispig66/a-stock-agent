"""Validate config/jobs.yaml schema and data integrity."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
YAML_PATH = ROOT / "config" / "jobs.yaml"

EXPECTED_JOB_COUNT = 7
REQUIRED_JOB_FIELDS = {"name", "skill", "schedule", "expected_output", "timing"}
REQUIRED_SCHEDULE_FIELDS = {"rrule", "cron", "hour", "minute"}
REQUIRED_AGENT_FIELDS = {"cli", "scheduling"}
KNOWN_SCHEDULING_TYPES = {"config-file", "cli-register", "launchd-fallback"}


def load():
    with open(YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_jobs_yaml_exists():
    assert YAML_PATH.exists()


def test_top_level_structure():
    cfg = load()
    assert "agent" in cfg
    assert "agents" in cfg
    assert "jobs" in cfg
    assert isinstance(cfg["agents"], dict)
    assert isinstance(cfg["jobs"], dict)


def test_expected_job_count():
    cfg = load()
    assert len(cfg["jobs"]) == EXPECTED_JOB_COUNT


def test_all_jobs_have_required_fields():
    cfg = load()
    for job_id, job in cfg["jobs"].items():
        missing = REQUIRED_JOB_FIELDS - set(job.keys())
        assert not missing, f"job '{job_id}' 缺少字段: {missing}"


def test_all_jobs_have_valid_schedules():
    cfg = load()
    for job_id, job in cfg["jobs"].items():
        sched = job["schedule"]
        missing = REQUIRED_SCHEDULE_FIELDS - set(sched.keys())
        assert not missing, f"job '{job_id}' schedule 缺少字段: {missing}"
        assert isinstance(sched["hour"], int)
        assert isinstance(sched["minute"], int)
        assert 0 <= sched["hour"] <= 23
        assert 0 <= sched["minute"] <= 59


def test_all_referenced_skills_exist():
    cfg = load()
    for job_id, job in cfg["jobs"].items():
        skill = job["skill"]
        skill_dir = ROOT / ".agents" / "skills" / skill
        assert skill_dir.is_dir(), f"job '{job_id}': skill 目录不存在 {skill_dir}"
        skill_md = skill_dir / "SKILL.md"
        assert skill_md.exists(), f"job '{job_id}': SKILL.md 不存在 {skill_md}"


def test_all_agents_have_required_fields():
    cfg = load()
    for name, agent in cfg["agents"].items():
        missing = REQUIRED_AGENT_FIELDS - set(agent.keys())
        assert not missing, f"agent '{name}' 缺少字段: {missing}"


def test_all_agents_have_known_scheduling_type():
    cfg = load()
    for name, agent in cfg["agents"].items():
        stype = agent["scheduling"]["type"]
        assert stype in KNOWN_SCHEDULING_TYPES, (
            f"agent '{name}': 未知 scheduling.type '{stype}'")


def test_default_agent_exists_in_agents():
    cfg = load()
    assert cfg["agent"] in cfg["agents"]


def test_expected_agents_present():
    cfg = load()
    expected = {"codex", "claude-code", "cline", "openclaw", "hermes", "opencode", "kimicode"}
    assert expected == set(cfg["agents"].keys())

"""Test config/jobs_loader.py install/uninstall/verify for each scheduling type."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

# Allow import from project root
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.jobs_loader import (
    load_config,
    active_agent_name,
    _cron_weekdays,
    install_jobs,
    uninstall_jobs,
    verify_jobs,
    validate,
    render_prompt,
    job_list,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return load_config()


def test_validate_passes(cfg):
    errors = validate(cfg)
    assert errors == [], errors


def test_active_agent_default_is_codex(cfg):
    assert active_agent_name(cfg) == "codex"


def test_active_agent_env_override(cfg, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT", "hermes")
    cfg2 = load_config()
    assert active_agent_name(cfg2) == "hermes"


def test_job_list_returns_7(cfg):
    assert len(job_list(cfg)) == 7


def test_render_prompt_codex(cfg):
    job = cfg["jobs"]["stock-premarket"]
    prompt = render_prompt(job, "codex")
    assert "Use the stock-premarket skill" in prompt
    assert "push.py" in prompt
    assert "CARD_VALIDATOR_MODE=enforce" in prompt


def test_render_prompt_generic(cfg):
    job = cfg["jobs"]["stock-premarket"]
    prompt = render_prompt(job, "claude-code")
    assert "SKILL.md" in prompt
    assert str(ROOT) in prompt
    assert "push.py" in prompt


# --- config-file: Codex TOML ---

def test_install_codex_toml(tmp_path, cfg):
    install_jobs(cfg, agent_override="codex", dry_run=True, output_dir=tmp_path)
    job_ids = sorted(cfg["jobs"].keys())
    assert sorted(p.name for p in tmp_path.iterdir()) == job_ids
    for job_id in job_ids:
        toml_path = tmp_path / job_id / "automation.toml"
        assert toml_path.exists()
        with open(toml_path, "rb") as f:
            d = tomllib.load(f)
        assert d["id"] == job_id
        assert d["kind"] == "cron"
        assert d["status"] == "ACTIVE"
        assert str(ROOT) in d["cwds"]


def test_uninstall_codex_toml(tmp_path, cfg):
    install_jobs(cfg, agent_override="codex", dry_run=True, output_dir=tmp_path)
    assert any(tmp_path.iterdir())
    uninstall_jobs(cfg, agent_override="codex", dry_run=True)


# --- config-file: Claude Code SKILL.md ---

def test_install_claude_code_skill_md(tmp_path, cfg):
    install_jobs(cfg, agent_override="claude-code", dry_run=True, output_dir=tmp_path)
    job_ids = sorted(cfg["jobs"].keys())
    assert sorted(p.name for p in tmp_path.iterdir()) == job_ids
    for job_id in job_ids:
        skill_md = tmp_path / job_id / "SKILL.md"
        assert skill_md.exists()
        content = skill_md.read_text(encoding="utf-8")
        assert f"name: {job_id}" in content
        assert "SKILL.md" in content


# --- launchd-fallback ---

def test_install_launchd_fallback(tmp_path, cfg):
    install_jobs(cfg, agent_override="opencode", dry_run=True, output_dir=tmp_path)
    for job_id in cfg["jobs"]:
        label = f"com.user.stockagent.{job_id}"
        plist = tmp_path / f"{label}.plist"
        assert plist.exists(), f"Missing {plist}"
        content = plist.read_text(encoding="utf-8")
        assert "run_agent_job.sh" in content


def test_cron_weekdays_supports_sunday_aliases():
    assert _cron_weekdays("0 21 * * 0") == [0]
    assert _cron_weekdays("0 21 * * 7") == [0]
    assert _cron_weekdays("0 8 * * 1-5") == [1, 2, 3, 4, 5]
    assert _cron_weekdays("0 8 * * *") is None


def test_cron_weekdays_rejects_invalid_values():
    with pytest.raises(ValueError, match="weekday"):
        _cron_weekdays("0 8 * * 8")
    with pytest.raises(ValueError, match="weekday"):
        _cron_weekdays("0 8 * * 5-1")


def test_cli_register_install_failure_exits_nonzero(cfg, monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=2, stderr="boom")

    monkeypatch.setattr("shutil.which", lambda _: "/tmp/fake-cli")
    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(SystemExit) as exc:
        install_jobs(cfg, agent_override="cline", dry_run=False)

    assert exc.value.code == 1


def test_cli_register_uninstall_failure_exits_nonzero(cfg, monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=2, stderr="boom")

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(SystemExit) as exc:
        uninstall_jobs(cfg, agent_override="cline", dry_run=False)

    assert exc.value.code == 1


def test_launchd_bootstrap_failure_exits_nonzero(tmp_path, cfg, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "bootstrap" in args:
            return subprocess.CompletedProcess(args=args, returncode=5, stderr="boom")
        return subprocess.CompletedProcess(args=args, returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(SystemExit) as exc:
        install_jobs(
            cfg,
            agent_override="opencode",
            dry_run=False,
            output_dir=tmp_path,
        )

    assert exc.value.code == 1
    assert any("bootstrap" in call for call in calls)


# --- verify ---

def test_verify_codex_after_install(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    cfg2 = load_config()
    out_dir = tmp_path / "codex" / "automations"
    install_jobs(cfg2, agent_override="codex", dry_run=False, output_dir=out_dir)
    errors = verify_jobs(cfg2, agent_override="codex")
    config_errors = [e for e in errors if "配置文件缺失" in e]
    assert config_errors == [], config_errors

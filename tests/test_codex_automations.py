from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "scripts" / "install_automations.sh"

EXPECTED_JOBS = {
    "stock-premarket": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0",
    "stock-intraday-09-30": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=30",
    "stock-intraday-09-45": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=45",
    "stock-intraday-11-30": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=11;BYMINUTE=30",
    "stock-intraday-14-30": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=14;BYMINUTE=30",
    "stock-postmarket": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=35",
    "stock-weekly-review": "FREQ=WEEKLY;INTERVAL=1;BYDAY=SU;BYHOUR=21;BYMINUTE=0",
}

EXPECTED_SKILLS = {
    "stock-premarket": "stock-premarket",
    "stock-intraday-09-30": "stock-intraday",
    "stock-intraday-09-45": "stock-intraday",
    "stock-intraday-11-30": "stock-intraday",
    "stock-intraday-14-30": "stock-intraday",
    "stock-postmarket": "stock-postmarket",
    "stock-weekly-review": "stock-weekly",
}


def run_installer(output_dir: Path, agent: str = "codex") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(output_dir.parent / "codex-home")
    env["CLAUDE_CONFIG_DIR"] = str(output_dir.parent / "claude-home")
    env["CODEX_AUTOMATION_MODEL"] = "gpt-5.4"
    env["CODEX_AUTOMATION_REASONING_EFFORT"] = "medium"
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "install", "--agent", agent,
         "--dry-run", "--output-dir", str(output_dir)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def run_default_dry_run(tmp_path: Path, agent: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(tmp_path / "codex-home")
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude-home")
    env["CODEX_AUTOMATION_MODEL"] = "gpt-5.4"
    env["CODEX_AUTOMATION_REASONING_EFFORT"] = "medium"
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "install", "--agent", agent, "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def result_details(result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"exit code: {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result_details(result)


def load_job(output_dir: Path, job_id: str, result: subprocess.CompletedProcess[str]) -> dict:
    path = output_dir / job_id / "automation.toml"
    assert path.exists(), (
        f"Missing automation TOML for {job_id}: {path}\n"
        f"{result_details(result)}"
    )
    data = path.read_bytes()
    return tomllib.loads(data.decode("utf-8"))


def test_codex_automation_dry_run_generates_all_jobs(tmp_path):
    out_dir = tmp_path / "automations"
    result = run_installer(out_dir)
    assert_success(result)
    assert "[dry-run]" in result.stdout
    assert out_dir.exists(), result_details(result)
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(EXPECTED_JOBS)


def test_codex_dry_run_without_output_dir_does_not_write_agent_home(tmp_path):
    result = run_default_dry_run(tmp_path, "codex")
    assert_success(result)
    assert "temporary output dir" in result.stdout
    assert not (tmp_path / "codex-home").exists()


def test_claude_code_dry_run_without_output_dir_does_not_write_agent_home(tmp_path):
    result = run_default_dry_run(tmp_path, "claude-code")
    assert_success(result)
    assert "temporary output dir" in result.stdout
    assert not (tmp_path / "claude-home").exists()


def test_codex_automation_toml_contract(tmp_path):
    out_dir = tmp_path / "automations"
    result = run_installer(out_dir)
    assert_success(result)

    for job_id, rrule in EXPECTED_JOBS.items():
        job = load_job(out_dir, job_id, result)
        assert job["version"] == 1
        assert job["id"] == job_id
        assert job["kind"] == "cron"
        assert job["status"] == "ACTIVE"
        assert job["rrule"] == rrule
        assert job["model"] == "gpt-5.4"
        assert job["reasoning_effort"] == "medium"
        assert job["execution_environment"] == "local"
        assert job["cwds"] == [str(ROOT)]
        assert isinstance(job["created_at"], int)
        assert isinstance(job["updated_at"], int)


def test_codex_automation_prompts_have_unattended_contract(tmp_path):
    out_dir = tmp_path / "automations"
    result = run_installer(out_dir)
    assert_success(result)

    for job_id in EXPECTED_JOBS:
        prompt = load_job(out_dir, job_id, result)["prompt"]
        assert "Required behavior:" in prompt
        assert "Failure handling:" in prompt
        assert "Final response:" in prompt
        assert "do not claim success" in prompt
        assert "push.py" in prompt
        assert "CARD_VALIDATOR_MODE=enforce" in prompt
        assert "do not send a card while validation is only warning" in prompt
        assert EXPECTED_SKILLS[job_id] in prompt
        assert ("cl" + "aude -p") not in prompt


def test_codex_automation_installer_summary_lists_jobs(tmp_path):
    out_dir = tmp_path / "automations"
    result = run_installer(out_dir)
    assert_success(result)
    for job_id in EXPECTED_JOBS:
        assert job_id in result.stdout


def test_claude_code_dry_run_generates_skill_md(tmp_path):
    out_dir = tmp_path / "claude-tasks"
    result = run_installer(out_dir, agent="claude-code")
    assert_success(result)
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(EXPECTED_JOBS)
    for job_id in EXPECTED_JOBS:
        skill_md = out_dir / job_id / "SKILL.md"
        assert skill_md.exists(), f"Missing SKILL.md for {job_id}"
        content = skill_md.read_text(encoding="utf-8")
        assert f"name: {job_id}" in content
        assert EXPECTED_SKILLS[job_id] in content
        assert "SKILL.md" in content
        assert "push.py" in content


def test_launchd_fallback_generates_plists(tmp_path):
    out_dir = tmp_path / "plists"
    result = run_installer(out_dir, agent="opencode")
    assert_success(result)
    for job_id in EXPECTED_JOBS:
        label = f"com.user.stockagent.{job_id}"
        plist = out_dir / f"{label}.plist"
        assert plist.exists(), f"Missing plist for {job_id}"
        content = plist.read_text(encoding="utf-8")
        assert label in content
        assert "run_agent_job.sh" in content
        assert job_id in content
        assert "STOCK_AGENT" in content
        assert "opencode" in content


def test_launchd_fallback_preserves_weekly_schedule(tmp_path):
    out_dir = tmp_path / "plists"
    result = run_installer(out_dir, agent="opencode")
    assert_success(result)

    weekly = out_dir / "com.user.stockagent.stock-weekly-review.plist"
    content = weekly.read_text(encoding="utf-8")
    assert "<key>Weekday</key><integer>0</integer>" in content

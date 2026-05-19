from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_codex_automations.sh"

EXPECTED_JOBS = {
    "stock-premarket": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=30",
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


def run_installer(output_dir: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(output_dir.parent / "home")
    env["CODEX_HOME"] = str(output_dir.parent / "codex-home")
    env["CODEX_AUTOMATIONS_DIR"] = str(output_dir)
    env["CODEX_AUTOMATION_MODEL"] = "gpt-5.4"
    env["CODEX_AUTOMATION_REASONING_EFFORT"] = "medium"
    return subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", *extra_args],
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
        assert EXPECTED_SKILLS[job_id] in prompt
        assert "claude -p" not in prompt


def test_codex_automation_installer_summary_lists_jobs(tmp_path):
    out_dir = tmp_path / "automations"
    result = run_installer(out_dir)

    assert_success(result)
    for job_id in EXPECTED_JOBS:
        assert job_id in result.stdout
    assert "Installed Codex automations under" in result.stdout

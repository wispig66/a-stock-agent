from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_script(relative_path: str) -> str:
    path = ROOT / relative_path
    assert path.exists(), f"Expected runtime script to exist: {relative_path}"
    return path.read_text()


def assert_contains_all(text: str, expected: list[str], *, label: str = "text") -> None:
    missing = [item for item in expected if item not in text]
    assert not missing, f"{label} missing expected text: {missing}"


def assert_contains_none(text: str, forbidden: list[str], *, label: str = "text") -> None:
    present = [item for item in forbidden if item in text]
    assert not present, f"{label} has unexpected text: {present}"


def test_setup_delegates_runtime_and_codex_installation() -> None:
    script = read_script("scripts/setup.sh")

    assert_contains_all(
        script,
        [
            "scripts/install_runtime_services.sh",
            "scripts/install_codex_automations.sh",
        ],
    )
    assert_contains_none(
        script,
        [
            "command -v claude",
            "launchctl bootstrap",
        ],
    )


def test_runtime_services_installer_only_handles_long_running_templates() -> None:
    script = read_script("scripts/install_runtime_services.sh")

    assert_contains_all(
        script,
        [
            "stockwatchloop",
            "stockanomalyloop",
            "stockthemeloop",
        ],
        label="scripts/install_runtime_services.sh",
    )
    for short_job in ["stockpremarket", "stockintraday", "stockpostmarket", "stockweekly"]:
        assert f'"com.user.{short_job}"' not in script
        assert f"'com.user.{short_job}'" not in script


def test_remote_codex_deploy_uses_git_and_runtime_helpers() -> None:
    script = read_script("scripts/deploy_remote_codex.sh")

    assert_contains_all(
        script,
        [
            "deploy.remote.env",
            "REMOTE_HOST",
            "REMOTE_ROOT",
            "REMOTE_REPO_URL",
            "REMOTE_BRANCH",
            "REMOTE_RUN_TESTS",
            'ssh "$REMOTE_HOST" "bash -s"',
            '[ ! -d "$REMOTE_ROOT/.git" ]',
            "git clone",
            "git fetch origin",
            "git checkout",
            "git pull --ff-only",
            "bash scripts/setup.sh",
            "scripts/sync_codex_skills.sh",
            "scripts/install_runtime_services.sh",
            "scripts/install_codex_automations.sh",
            "scripts/disable_legacy_claude_launchd.sh",
            "scripts/doctor_codex_runtime.sh",
            "uv run pytest tests/",
            "Remote deployment summary:",
        ],
        label="scripts/deploy_remote_codex.sh",
    )
    assert "rsync" not in script


def test_runtime_doctor_checks_without_sending_real_telegram_push() -> None:
    script = read_script("scripts/doctor_codex_runtime.sh")

    assert_contains_all(
        script,
        [
            "TG_BOT_TOKEN",
            ".agents/skills",
            "stock-premarket",
            "stock-intraday",
            "stock-postmarket",
            "stock-weekly",
            "data/daily.db",
            "push_log",
            "data/trade_calendar.csv",
            "automations",
            "cwd",
            "PROJECT_ROOT",
            "launchctl list",
            "command -v uv",
            "command -v sqlite3",
            ".agents/skills/$skill/SKILL.md",
            "sqlite_master",
            "cwds =",
            "fail \"legacy short LLM launchd job still loaded",
            "com.user.stockpremarket",
            "com.user.stockintraday",
            "com.user.stockpostmarket",
            "com.user.stockweekly",
        ],
        label="scripts/doctor_codex_runtime.sh",
    )
    assert_contains_none(
        script,
        [
            "notify.py test",
            "sendMessage",
        ],
        label="scripts/doctor_codex_runtime.sh",
    )


def test_new_runtime_shell_scripts_parse_with_bash_n() -> None:
    scripts = [
        "scripts/install_runtime_services.sh",
        "scripts/deploy_remote_codex.sh",
        "scripts/doctor_codex_runtime.sh",
    ]

    for relative_path in scripts:
        path = ROOT / relative_path
        result = subprocess.run(
            ["bash", "-n", str(path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, (
            f"{relative_path} failed bash -n with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

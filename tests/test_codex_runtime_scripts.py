from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_script(relative_path: str) -> str:
    path = ROOT / relative_path
    assert path.exists(), f"Expected runtime script to exist: {relative_path}"
    return path.read_text()


def assert_contains_all(text: str, expected: list[str]) -> None:
    missing = [item for item in expected if item not in text]
    assert not missing, f"Missing expected text: {missing}"


def assert_contains_none(text: str, forbidden: list[str]) -> None:
    present = [item for item in forbidden if item in text]
    assert not present, f"Unexpected text found: {present}"


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
    )
    assert_contains_none(
        script,
        [
            "stockpremarket",
            "stockintraday",
            "stockpostmarket",
            "stockweekly",
        ],
    )


def test_remote_codex_deploy_uses_git_and_runtime_helpers() -> None:
    script = read_script("scripts/deploy_remote_codex.sh")

    assert_contains_all(
        script,
        [
            "deploy.remote.env",
            "git clone",
            "git pull --ff-only",
            "scripts/install_codex_automations.sh",
            "scripts/doctor_codex_runtime.sh",
        ],
    )
    assert "rsync" not in script


def test_runtime_doctor_checks_without_sending_real_telegram_push() -> None:
    script = read_script("scripts/doctor_codex_runtime.sh")

    assert_contains_all(
        script,
        [
            "TG_BOT_TOKEN",
            "automations",
            "launchctl list",
        ],
    )
    assert_contains_none(
        script,
        [
            "notify.py test",
            "sendMessage",
        ],
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

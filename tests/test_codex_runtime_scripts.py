from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEX_JOBS = [
    "stock-premarket",
    "stock-intraday-09-30",
    "stock-intraday-09-45",
    "stock-intraday-11-30",
    "stock-intraday-14-30",
    "stock-postmarket",
    "stock-weekly-review",
]


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


def effective_shell_lines(script: str) -> list[str]:
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("echo ")
    ]


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def base_env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    return env


def make_runtime_project(tmp_path: Path) -> Path:
    project = tmp_path / "runtime-project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "doctor_codex_runtime.sh", project / "scripts" / "doctor_codex_runtime.sh")
    (project / ".agents" / "skills").mkdir(parents=True)
    for skill in ["stock-premarket", "stock-intraday", "stock-postmarket", "stock-weekly"]:
        skill_dir = project / ".agents" / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
    (project / "data").mkdir()
    subprocess.run(
        [
            "sqlite3",
            str(project / "data" / "daily.db"),
            "CREATE TABLE push_log(id INTEGER PRIMARY KEY);",
        ],
        check=True,
    )
    (project / "data" / "trade_calendar.csv").write_text("cal_date,is_open\n20260519,1\n", encoding="utf-8")
    (project / ".env").write_text("TG_BOT_TOKEN=test-token\nTG_CHAT_ID=test-chat\n", encoding="utf-8")
    return project


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
            "scripts/install_launchd.sh",
            "notify.py test",
            "LaunchAgents",
            "launchctl print",
            "launchctl bootout",
            "claude --version",
            "Telegram 推送通了",
        ],
    )


def test_runtime_services_installer_only_bootstraps_long_running_templates(tmp_path) -> None:
    script = read_script("scripts/install_runtime_services.sh")
    env = base_env(tmp_path)
    log = tmp_path / "launchctl.log"
    env["LAUNCHCTL_LOG"] = str(log)
    write_executable(
        tmp_path / "bin" / "launchctl",
        """#!/usr/bin/env bash
echo "$@" >> "$LAUNCHCTL_LOG"
if [ "${1:-}" = "print" ]; then
    exit 1
fi
exit 0
""",
    )

    assert_contains_all(
        script,
        [
            "stockwatchloop",
            "stockanomalyloop",
            "stockthemeloop",
        ],
        label="scripts/install_runtime_services.sh",
    )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install_runtime_services.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    launchctl_log = log.read_text(encoding="utf-8")
    bootstrapped = [
        Path(line.split()[-1]).stem
        for line in launchctl_log.splitlines()
        if line.startswith("bootstrap ")
    ]
    assert bootstrapped == [
        "com.user.stockwatchloop",
        "com.user.stockanomalyloop",
        "com.user.stockthemeloop",
    ]


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
            'cd "$REMOTE_ROOT"',
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


def test_remote_codex_deploy_sends_pull_based_payload_to_ssh(tmp_path) -> None:
    read_script("scripts/deploy_remote_codex.sh")
    env = base_env(tmp_path)
    ssh_args = tmp_path / "ssh.args"
    ssh_payload = tmp_path / "ssh.payload"
    write_executable(
        tmp_path / "bin" / "ssh",
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" > {ssh_args}
cat > {ssh_payload}
""",
    )
    config = tmp_path / "deploy.remote.env"
    config.write_text(
        "\n".join(
            [
                "REMOTE_HOST=tester@example-host",
                f"REMOTE_ROOT={tmp_path / 'remote-stock'}",
                "REMOTE_REPO_URL=https://example.com/org/stock.git",
                "REMOTE_BRANCH=codex-test",
                "REMOTE_RUN_TESTS=1",
            ]
        ),
        encoding="utf-8",
    )
    env["DEPLOY_REMOTE_ENV"] = str(config)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "deploy_remote_codex.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert ssh_args.read_text(encoding="utf-8").strip() == 'tester@example-host bash -s'
    payload = ssh_payload.read_text(encoding="utf-8")
    payload_syntax = subprocess.run(
        ["bash", "-n", str(ssh_payload)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert payload_syntax.returncode == 0, payload_syntax.stderr
    assert f'REMOTE_ROOT="{tmp_path / "remote-stock"}"' in payload
    assert 'REMOTE_REPO_URL="https://example.com/org/stock.git"' in payload
    assert 'REMOTE_BRANCH="codex-test"' in payload
    assert 'REMOTE_RUN_TESTS="1"' in payload
    expected_sequence = [
        'if [ ! -d "$REMOTE_ROOT/.git" ]; then',
        'git clone "$REMOTE_REPO_URL" "$REMOTE_ROOT"',
        'cd "$REMOTE_ROOT"',
        'git fetch origin "$REMOTE_BRANCH"',
        'git checkout "$REMOTE_BRANCH"',
        'git pull --ff-only origin "$REMOTE_BRANCH"',
        "bash scripts/setup.sh",
        "bash scripts/sync_codex_skills.sh",
        "bash scripts/install_codex_automations.sh",
        "bash scripts/install_runtime_services.sh",
        "bash scripts/disable_legacy_claude_launchd.sh",
        "bash scripts/doctor_codex_runtime.sh",
        "uv run pytest tests/",
    ]
    effective_lines = "\n".join(effective_shell_lines(payload))
    positions = []
    for item in expected_sequence:
        assert item in effective_lines, f"missing remote deploy payload command: {item}"
        positions.append(effective_lines.index(item))
    assert positions == sorted(positions)
    clone_pos = effective_lines.index('git clone "$REMOTE_REPO_URL" "$REMOTE_ROOT"')
    guard_end_pos = effective_lines.index("\nfi", clone_pos)
    assert clone_pos < guard_end_pos < effective_lines.index('cd "$REMOTE_ROOT"')
    assert "rsync" not in payload
    assert "Remote deployment summary:" in payload


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


def prepare_codex_home(home: Path, project_root: Path) -> None:
    for job in CODEX_JOBS:
        job_dir = home / ".codex" / "automations" / job
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "automation.toml").write_text(
            f'cwds = ["{project_root}"]\n',
            encoding="utf-8",
        )


def write_launchctl_fake(fake_bin: Path, *, loaded_label: str | None = None) -> None:
    loaded_case = (
        "\n".join(
            [
                f'if [ "$*" = "print gui/$(id -u)/{loaded_label}" ]; then exit 0; fi',
                f'if [ "${{1:-}}" = "list" ]; then echo "123 0 {loaded_label}"; exit 0; fi',
            ]
        )
        if loaded_label
        else 'if [ "${1:-}" = "list" ]; then exit 0; fi'
    )
    write_executable(
        fake_bin / "launchctl",
        f"""#!/usr/bin/env bash
{loaded_case}
if [ "${{1:-}}" = "print" ]; then
    exit 1
fi
exit 0
""",
    )


def test_runtime_doctor_executes_readiness_checks_without_real_push(tmp_path) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    env["PROJECT_ROOT"] = str(project)
    home = Path(env["HOME"])
    prepare_codex_home(home, project)
    write_launchctl_fake(tmp_path / "bin")

    result = subprocess.run(
        ["bash", str(project / "scripts" / "doctor_codex_runtime.sh")],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "TG_BOT_TOKEN" in result.stdout
    assert "skill stock-premarket exists" in result.stdout
    assert "daily.db push_log table exists" in result.stdout
    assert "automation stock-premarket cwd ok" in result.stdout
    assert "legacy short LLM launchd job not loaded: com.user.stockpremarket" in result.stdout
    assert "notify.py test" not in result.stdout
    assert "sendMessage" not in result.stdout


def test_runtime_doctor_fails_when_legacy_short_launchd_is_loaded(tmp_path) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    env["PROJECT_ROOT"] = str(project)
    home = Path(env["HOME"])
    prepare_codex_home(home, project)
    write_launchctl_fake(tmp_path / "bin", loaded_label="com.user.stockpremarket")

    result = subprocess.run(
        ["bash", str(project / "scripts" / "doctor_codex_runtime.sh")],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "legacy short LLM launchd job still loaded: com.user.stockpremarket" in result.stderr


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

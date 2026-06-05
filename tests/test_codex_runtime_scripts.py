from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


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


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def base_env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    codex = fake_bin / "codex"
    codex.write_text("#!/usr/bin/env bash\necho codex-test\n", encoding="utf-8")
    codex.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["STOCK_DOCTOR_SKIP_NETWORK"] = "1"
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
    (project / ".env").write_text("FEISHU_APP_ID=test-app\nFEISHU_APP_SECRET=test-secret\n", encoding="utf-8")
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
            "launchctl bootstrap",
            "scripts/install_launchd.sh",
            "notify.py test",
            "LaunchAgents",
            "launchctl print",
            "launchctl bootout",
            "Telegram 推送通了",
        ],
    )


def test_quickstart_installs_initializes_and_starts_gateway() -> None:
    script = read_script("scripts/quickstart.sh")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert_contains_all(
        readme,
        [
            "三分钟快速开始",
            "请帮我在这台机器上运行 A Stock Agent 快速开始",
            "不要让我手动复制命令",
            "飞书",
        ],
        label="README quickstart",
    )
    assert_contains_all(
        script,
        [
            "uv sync --group dev",
            "stock_codex/schema/init_db.sql",
            "scripts/migrate_channels.py",
            "scripts/start_gateway.sh",
            "--install-schedule",
            "--with-feishu",
        ],
        label="scripts/quickstart.sh",
    )
    assert_contains_none(
        script,
        [
            "scripts/set_tg_commands.py",
            "scripts/start_tg_listener.sh",
            "TG_BOT_TOKEN",
        ],
        label="scripts/quickstart.sh",
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
            "stockmarketdynamic",
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

def test_runtime_services_installer_bootstraps_market_dynamic_only_when_opted_in(tmp_path) -> None:
    env = base_env(tmp_path)
    log = tmp_path / "launchctl.log"
    env["LAUNCHCTL_LOG"] = str(log)
    env["ENABLE_MARKET_DYNAMIC_LAUNCHD"] = "1"
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
    assert "com.user.stockmarketdynamic.plist" in launchctl_log


def test_runtime_doctor_checks_without_sending_real_telegram_push() -> None:
    script = read_script("scripts/doctor_codex_runtime.sh")

    assert_contains_all(
        script,
        [
            "FEISHU_APP_ID",
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
            "DNS_REQUIRED_HOSTS",
            "open.feishu.cn",
            "push2ex.eastmoney.com",
            "q.10jqka.com.cn",
            "check_network_readiness",
            "check_https_reachable \"https://open.feishu.cn\"",
            "STOCK_DOCTOR_SKIP_NETWORK",
            ".agents/skills/$skill/SKILL.md",
            "sqlite_master",
            "tomllib",
            "cwds must be an array",
            "fail \"legacy short LLM launchd job still loaded",
            "legacy short LLM launchd plist still installed",
            "$key missing or empty in .env",
            ".env missing; $key is required",
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
    assert "FEISHU_APP_ID" in result.stdout
    assert "skill stock-premarket exists" in result.stdout
    assert "daily.db push_log table exists" in result.stdout
    assert "automation stock-premarket cwd ok" in result.stdout
    assert "legacy short LLM launchd job not loaded: com.user.stockpremarket" in result.stdout
    assert "legacy short LLM launchd plist absent:" in result.stdout
    assert "network readiness checks skipped by STOCK_DOCTOR_SKIP_NETWORK=1" in result.stderr
    assert "notify.py test" not in result.stdout
    assert "sendMessage" not in result.stdout


@pytest.mark.parametrize(
    ("env_text", "expected_error"),
    [
        ("", "FEISHU_APP_ID missing or empty in .env"),
        ("FEISHU_APP_ID=test-app\n", "FEISHU_APP_SECRET missing or empty in .env"),
        ("FEISHU_APP_ID=\nFEISHU_APP_SECRET=test-secret\n", "FEISHU_APP_ID missing or empty in .env"),
        ('FEISHU_APP_ID=""\nFEISHU_APP_SECRET=test-secret\n', "FEISHU_APP_ID missing or empty in .env"),
        ("FEISHU_APP_ID=''\nFEISHU_APP_SECRET=test-secret\n", "FEISHU_APP_ID missing or empty in .env"),
        ('FEISHU_APP_ID=test-app\nFEISHU_APP_SECRET=""\n', "FEISHU_APP_SECRET missing or empty in .env"),
    ],
)
def test_runtime_doctor_fails_when_required_env_values_are_missing_or_empty(
    tmp_path, env_text: str, expected_error: str
) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    (project / ".env").write_text(env_text, encoding="utf-8")
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

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_runtime_doctor_fails_when_env_file_is_missing(tmp_path) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    (project / ".env").unlink()
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

    assert result.returncode != 0
    assert ".env missing; FEISHU_APP_ID is required" in result.stderr


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


def test_runtime_doctor_fails_when_legacy_short_launchd_plist_file_remains(tmp_path) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    env["PROJECT_ROOT"] = str(project)
    home = Path(env["HOME"])
    prepare_codex_home(home, project)
    stale = home / "Library" / "LaunchAgents" / "com.user.stockpremarket.plist"
    stale.parent.mkdir(parents=True)
    stale.write_text("<plist/>", encoding="utf-8")
    write_launchctl_fake(tmp_path / "bin")

    result = subprocess.run(
        ["bash", str(project / "scripts" / "doctor_codex_runtime.sh")],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "legacy short LLM launchd plist still installed" in result.stderr


def test_disable_legacy_llm_launchd_removes_stale_launchagent_plists(tmp_path) -> None:
    script = read_script("scripts/disable_legacy_llm_launchd.sh")
    assert_contains_all(
        script,
        [
            "launchctl bootout",
            "rm -f \"$target\"",
            "com.user.stockpremarket",
            "com.user.stockweekly",
        ],
        label="scripts/disable_legacy_llm_launchd.sh",
    )
    assert "|| true" not in script

    env = base_env(tmp_path)
    home = Path(env["HOME"])
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in [
        "com.user.stockpremarket",
        "com.user.stockintraday",
        "com.user.stockpostmarket",
        "com.user.stockweekly",
    ]:
        (agents / f"{label}.plist").write_text("<plist/>", encoding="utf-8")
    log = tmp_path / "launchctl.log"
    write_executable(
        tmp_path / "bin" / "launchctl",
        f"""#!/usr/bin/env bash
echo "$@" >> {log}
if [ "${{1:-}}" = "print" ]; then
    exit 1
fi
if [ "${{1:-}}" = "list" ]; then
    exit 0
fi
exit 0
""",
    )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "disable_legacy_llm_launchd.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not list(agents.glob("com.user.stock*.plist"))


@pytest.mark.parametrize("job_id", CODEX_JOBS)
def test_runtime_doctor_fails_when_codex_automation_is_missing(tmp_path, job_id: str) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    env["PROJECT_ROOT"] = str(project)
    home = Path(env["HOME"])
    prepare_codex_home(home, project)
    shutil.rmtree(home / ".codex" / "automations" / job_id)
    write_launchctl_fake(tmp_path / "bin")

    result = subprocess.run(
        ["bash", str(project / "scripts" / "doctor_codex_runtime.sh")],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"automation {job_id} missing" in result.stderr


@pytest.mark.parametrize("job_id", CODEX_JOBS)
def test_runtime_doctor_fails_when_automation_cwd_points_elsewhere(tmp_path, job_id: str) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    env["PROJECT_ROOT"] = str(project)
    home = Path(env["HOME"])
    prepare_codex_home(home, project)
    (home / ".codex" / "automations" / job_id / "automation.toml").write_text(
        'cwds = ["/tmp/not-this-runtime"]\n',
        encoding="utf-8",
    )
    write_launchctl_fake(tmp_path / "bin")

    result = subprocess.run(
        ["bash", str(project / "scripts" / "doctor_codex_runtime.sh")],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"automation {job_id} cwd does not point to" in result.stderr


def test_runtime_doctor_fails_when_automation_cwd_only_appears_in_comment(tmp_path) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    env["PROJECT_ROOT"] = str(project)
    home = Path(env["HOME"])
    prepare_codex_home(home, project)
    (home / ".codex" / "automations" / "stock-premarket" / "automation.toml").write_text(
        f'# cwds = ["{project}"]\ncwds = ["/tmp/not-this-runtime"]\n',
        encoding="utf-8",
    )
    write_launchctl_fake(tmp_path / "bin")

    result = subprocess.run(
        ["bash", str(project / "scripts" / "doctor_codex_runtime.sh")],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "automation stock-premarket cwd does not point to" in result.stderr


@pytest.mark.parametrize(
    ("toml_text", "expected_error"),
    [
        ("name = \"stock-premarket\"\n", "cwd does not point to"),
        ("cwds = \"/tmp/not-an-array\"\n", "cwd does not point to"),
    ],
)
def test_runtime_doctor_fails_when_automation_cwds_is_missing_or_not_an_array(
    tmp_path, toml_text: str, expected_error: str
) -> None:
    read_script("scripts/doctor_codex_runtime.sh")
    env = base_env(tmp_path)
    project = make_runtime_project(tmp_path)
    env["PROJECT_ROOT"] = str(project)
    home = Path(env["HOME"])
    prepare_codex_home(home, project)
    (home / ".codex" / "automations" / "stock-premarket" / "automation.toml").write_text(
        toml_text,
        encoding="utf-8",
    )
    write_launchctl_fake(tmp_path / "bin")

    result = subprocess.run(
        ["bash", str(project / "scripts" / "doctor_codex_runtime.sh")],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_new_runtime_shell_scripts_parse_with_bash_n() -> None:
    scripts = [
        "scripts/install_runtime_services.sh",
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

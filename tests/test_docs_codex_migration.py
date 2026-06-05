from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def assert_contains_all(text: str, expected: list[str], *, label: str) -> None:
    missing = [term for term in expected if term not in text]
    assert not missing, f"{label} missing expected terms: {missing}"


def assert_contains_none(text: str, forbidden: list[str], *, label: str) -> None:
    present = [term for term in forbidden if term in text]
    assert not present, f"{label} contains forbidden terms: {present}"


def markdown_section(text: str, heading: str, *, exact: bool = True) -> str:
    lines = text.splitlines()
    if exact:
        matches = [index for index, line in enumerate(lines) if line == heading]
    else:
        matches = [index for index, line in enumerate(lines) if line.startswith(heading)]
    if not matches:
        raise AssertionError(f"missing markdown section: {heading}")

    start = matches[0]
    heading_level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.startswith("#"):
            continue
        line_level = len(line) - len(line.lstrip("#"))
        if line_level <= heading_level:
            end = index
            break
    return "\n".join(lines[start:end])


def markdown_table_containing(text: str, marker: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if marker not in line:
            continue
        end = index + 1
        while end < len(lines) and lines[end].startswith("|"):
            end += 1
        return "\n".join(lines[index:end])
    raise AssertionError(f"missing markdown table containing: {marker}")


def test_readme_describes_codex_as_short_llm_scheduler():
    text = read("README.md")
    schedule_section = markdown_section(text, "## 调度")
    active_schedule_table = markdown_table_containing(schedule_section, "| 类型 |")

    assert_contains_all(
        schedule_section,
        ["短时 LLM", "长时 daemon", "等价服务管理器"],
        label="README scheduling section",
    )
    assert_contains_none(
        active_schedule_table,
        [
            "com.user.stockpremarket.plist",
            "com.user.stockintraday.plist",
            "com.user.stockpostmarket.plist",
            "com.user.stockweekly.plist",
        ],
        label="README active schedule table",
    )
    assert_contains_all(
        text,
        [
            "bash scripts/install_automations.sh",
            "bash scripts/install_runtime_services.sh",
        ],
        label="README install instructions",
    )


def test_codex_runbook_documents_local_runtime_deploy():
    text = read("docs/automations.md")

    assert_contains_all(
        text,
        [
            "config/jobs.yaml",
            "install_automations.sh",
            "install_runtime_services.sh",
            "doctor_codex_runtime.sh",
            "scripts/start_gateway.sh",
            "com.user.stockchannelgateway",
        ],
        label="Codex automation runbook",
    )
    assert_contains_none(
        text,
        [
            "Telegram bot token",
            "push Telegram",
            "api.telegram.org",
            "com.user.stocktglistener",
        ],
        label="Codex automation runbook current IM docs",
    )


def test_im_gateway_runbook_documents_feishu_weixin_runtime():
    readme = read("README.md")
    runbook = read("docs/im_gateway.md")

    assert_contains_all(
        readme,
        ["docs/im_gateway.md", "CHANNELS_ENABLED=feishu,weixin", "scripts/configure_weixin.py"],
        label="README IM gateway section",
    )
    assert_contains_all(
        runbook,
        [
            "CHANNELS_ENABLED=feishu,weixin",
            "CHANNELS_NOTIFY=feishu,weixin",
            "WEIXIN_HOME_CHANNEL",
            "channel_outbox",
            "channel_outbound_log",
            "com.user.stockchannelgateway",
            "notify test",
        ],
        label="IM gateway runbook",
    )
    assert_contains_none(
        runbook,
        ["Telegram listener", "tg_listener", "stocktglistener"],
        label="IM gateway runbook",
    )


def test_runtime_docs_skills_and_templates_do_not_reference_personal_project_paths():
    forbidden = [
        "/Users/",
        "/Users/wispig/Desktop/a-stock-agent",
        "Desktop/a-stock-agent",
        "/Users/wispig/Desktop/stock",
        "~/Desktop/stock",
    ]
    paths = sorted(
        {
            *ROOT.glob("*.md"),
            *(ROOT / "docs").rglob("*.md"),
            *(ROOT / ".agents" / "skills").rglob("SKILL.md"),
            *(ROOT / "launchd").rglob("*.plist"),
        }
    )
    for path in paths:
        assert_contains_none(
            path.read_text(encoding="utf-8"),
            forbidden,
            label=str(path.relative_to(ROOT)),
        )


def test_validator_doc_mentions_codex_strategy_not_only_launchd():
    text = read("docs/card_validator_enforce_switch.md")
    enforce_section = markdown_section(text, "## 切换 enforce 操作", exact=False)
    codex_short_llm_section = markdown_section(
        enforce_section,
        "### Codex automation short LLM jobs",
    )

    assert_contains_all(
        enforce_section,
        ["Codex automation", "launchd daemon", "short LLM"],
        label="validator enforce section",
    )
    assert_contains_none(
        codex_short_llm_section,
        ["launchd/com.user.stockweekly.plist"],
        label="validator Codex short LLM section",
    )

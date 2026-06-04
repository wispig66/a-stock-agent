"""Multi-agent automation job loader.

Reads config/jobs.yaml, resolves the active agent, and provides
install / uninstall / verify operations for 3 scheduling types:
  - config-file  (Codex TOML, Claude Code SKILL.md)
  - cli-register (Cline, OpenClaw, Hermes)
  - launchd-fallback (OpenCode, KimiCode)

CLI usage:
    python config/jobs_loader.py install  [--agent X] [--dry-run] [--output-dir DIR]
    python config/jobs_loader.py uninstall [--agent X] [--dry-run]
    python config/jobs_loader.py verify   [--agent X]
    python config/jobs_loader.py show
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = ROOT / "config" / "jobs.yaml"

KNOWN_SCHEDULING_TYPES = {"config-file", "cli-register", "launchd-fallback"}
KNOWN_CONFIG_FORMATS = {"toml", "skill-md"}

LAUNCHD_LABEL_PREFIX = "com.user.stockagent"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or YAML_PATH
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    agent_override = os.environ.get("STOCK_AGENT")
    if agent_override:
        cfg["agent"] = agent_override
    return cfg


def active_agent_name(cfg: dict[str, Any], override: str | None = None) -> str:
    name = override or cfg["agent"]
    if name not in cfg["agents"]:
        sys.exit(f"错误: 未知 agent '{name}'。可选: {', '.join(cfg['agents'])}")
    return name


def active_agent(cfg: dict[str, Any], override: str | None = None) -> dict[str, Any]:
    return cfg["agents"][active_agent_name(cfg, override)]


def job_list(cfg: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return list(cfg["jobs"].items())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "agents" not in cfg or "jobs" not in cfg:
        errors.append("jobs.yaml 缺少 agents 或 jobs 顶层字段")
        return errors

    for name, agent in cfg["agents"].items():
        stype = agent.get("scheduling", {}).get("type", "")
        if stype not in KNOWN_SCHEDULING_TYPES:
            errors.append(f"agent '{name}': 未知 scheduling.type '{stype}'")
        if stype == "config-file":
            fmt = agent["scheduling"].get("format", "")
            if fmt not in KNOWN_CONFIG_FORMATS:
                errors.append(f"agent '{name}': 未知 config-file format '{fmt}'")

    for job_id, job in cfg["jobs"].items():
        skill = job.get("skill", "")
        skill_dir = ROOT / ".agents" / "skills" / skill
        if not skill_dir.is_dir():
            errors.append(f"job '{job_id}': skill 目录不存在 {skill_dir}")
        if "schedule" not in job:
            errors.append(f"job '{job_id}': 缺少 schedule")

    return errors


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _skill_prompt_codex(skill: str, expected_output: str, timing: str) -> str:
    return textwrap.dedent(f"""\
        Use the {skill} skill in this repository.

        Required behavior:
        1. Run the {skill} workflow from the current repository checkout.
        2. Produce the expected output: {expected_output}.
        3. Use the unified push.py path for any IM delivery required by the skill.
        4. Invoke push.py with CARD_VALIDATOR_MODE=enforce. If validation fails, fix the card and retry; do not send a card while validation is only warning.
        5. Keep the work unattended; do not ask the user to run commands or provide context that the repository already contains.

        Failure handling:
        If any required data source, command, validation step, or push.py delivery fails, report the concrete failure and do not claim success. Do not hide partial failures behind a normal summary.

        Final response:
        Return only a concise operational summary for {timing}, including whether files were updated and whether push.py delivered the card.""")


def _skill_prompt_generic(skill: str, expected_output: str, timing: str,
                          cwd: str) -> str:
    return textwrap.dedent(f"""\
        You are working in {cwd}.
        Read the file .agents/skills/{skill}/SKILL.md and follow every step exactly.

        Required behavior:
        1. Run the {skill} workflow as described in SKILL.md.
        2. Produce the expected output: {expected_output}.
        3. Use the unified push.py path for any IM delivery required by the skill.
        4. Invoke push.py with CARD_VALIDATOR_MODE=enforce. If validation fails, fix the card and retry; do not send a card while validation is only warning.
        5. Keep the work unattended; do not ask the user to run commands or provide context that the repository already contains.

        Failure handling:
        If any required data source, command, validation step, or push.py delivery fails, report the concrete failure and do not claim success. Do not hide partial failures behind a normal summary.

        Final response:
        Return only a concise operational summary for {timing}, including whether files were updated and whether push.py delivered the card.""")


def render_prompt(job: dict[str, Any], agent_name: str) -> str:
    skill = job["skill"]
    expected = job["expected_output"]
    timing = job["timing"]
    if agent_name == "codex":
        return _skill_prompt_codex(skill, expected, timing)
    return _skill_prompt_generic(skill, expected, timing, str(ROOT))


# ---------------------------------------------------------------------------
# Agent home resolution
# ---------------------------------------------------------------------------

def _resolve_agent_home(agent: dict[str, Any]) -> Path:
    env_cfg = agent.get("env", {})
    env_var = env_cfg.get("agent_home", "")
    default = env_cfg.get("agent_home_default", "")
    value = os.environ.get(env_var, "") if env_var else ""
    if not value:
        value = default
    return Path(os.path.expanduser(value))


# ---------------------------------------------------------------------------
# Install: config-file
# ---------------------------------------------------------------------------

def _install_config_file_toml(
    agent: dict[str, Any],
    jobs: list[tuple[str, dict[str, Any]]],
    agent_name: str,
    *,
    dry_run: bool,
    output_dir: Path | None,
) -> None:
    agent_home = _resolve_agent_home(agent)
    defaults = agent.get("defaults", {})
    model = os.environ.get("CODEX_AUTOMATION_MODEL", defaults.get("model", ""))
    reasoning = os.environ.get(
        "CODEX_AUTOMATION_REASONING_EFFORT",
        defaults.get("reasoning_effort", "medium"),
    )

    for job_id, job in jobs:
        prompt = render_prompt(job, agent_name)
        sched = agent["scheduling"]
        dir_template = sched["dir"]
        dest_dir = output_dir / job_id if output_dir else Path(
            os.path.expanduser(dir_template.format(
                agent_home=agent_home, job_id=job_id)))
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / sched["file"]

        ts = int(time.time() * 1000)
        lines = [
            'version = 1',
            f'id = {json.dumps(job_id)}',
            'kind = "cron"',
            f'name = {json.dumps(job["name"])}',
            f'prompt = {json.dumps(prompt)}',
            'status = "ACTIVE"',
            f'rrule = {json.dumps(job["schedule"]["rrule"])}',
            f'model = {json.dumps(model)}',
            f'reasoning_effort = {json.dumps(reasoning)}',
            'execution_environment = "local"',
            f'cwds = [{json.dumps(str(ROOT))}]',
            f'created_at = {ts}',
            f'updated_at = {ts}',
        ]
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tag = "[dry-run]" if dry_run else "[+]"
        print(f"{tag} generated {job_id}")


def _install_config_file_skill_md(
    agent: dict[str, Any],
    jobs: list[tuple[str, dict[str, Any]]],
    agent_name: str,
    *,
    dry_run: bool,
    output_dir: Path | None,
) -> None:
    agent_home = _resolve_agent_home(agent)

    for job_id, job in jobs:
        prompt = render_prompt(job, agent_name)
        sched = agent["scheduling"]
        dir_template = sched["dir"]
        dest_dir = output_dir / job_id if output_dir else Path(
            os.path.expanduser(dir_template.format(
                agent_home=agent_home, job_id=job_id)))
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / sched["file"]

        content = textwrap.dedent(f"""\
            ---
            name: {job_id}
            description: {job['name']}
            ---

            {prompt}
        """)
        dest.write_text(content, encoding="utf-8")
        tag = "[dry-run]" if dry_run else "[+]"
        print(f"{tag} generated {job_id}")


# ---------------------------------------------------------------------------
# Install: cli-register
# ---------------------------------------------------------------------------

def _install_cli_register(
    agent: dict[str, Any],
    jobs: list[tuple[str, dict[str, Any]]],
    agent_name: str,
    *,
    dry_run: bool,
) -> None:
    cli = agent["cli"]
    sched = agent["scheduling"]
    install_tpl = sched["install_cmd"]

    for job_id, job in jobs:
        prompt = render_prompt(job, agent_name)
        prompt_escaped = prompt.replace("'", "'\\''")
        cmd_str = f"{cli} {install_tpl}".format(
            job_id=job_id,
            cron=job["schedule"]["cron"],
            schedule=job["schedule"]["rrule"],
            prompt_escaped=prompt_escaped,
        )
        if dry_run:
            print(f"[dry-run] would run: {cmd_str[:120]}...")
        else:
            result = subprocess.run(
                cmd_str, shell=True, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                print(f"[!] {job_id} 注册失败: {result.stderr[:200]}", file=sys.stderr)
            else:
                print(f"[+] registered {job_id}")


# ---------------------------------------------------------------------------
# Install: launchd-fallback
# ---------------------------------------------------------------------------

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{runner}</string>
    <string>{job_id}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{cwd}</string>
  <key>StartCalendarInterval</key>
  {calendar_xml}
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>{cwd}/logs/agent_{job_id_safe}_stdout.log</string>
  <key>StandardErrorPath</key>
  <string>{cwd}/logs/agent_{job_id_safe}_stderr.log</string>
</dict>
</plist>"""


def _calendar_xml(job: dict[str, Any]) -> str:
    sched = job["schedule"]
    hour = sched["hour"]
    minute = sched["minute"]
    if sched.get("weekdays_only", False):
        entries = []
        for day in range(1, 6):  # Monday=1 .. Friday=5
            entries.append(textwrap.dedent(f"""\
                <dict>
                  <key>Weekday</key><integer>{day}</integer>
                  <key>Hour</key><integer>{hour}</integer>
                  <key>Minute</key><integer>{minute}</integer>
                </dict>"""))
        return "<array>\n" + "\n".join(entries) + "\n  </array>"
    return textwrap.dedent(f"""\
        <dict>
          <key>Hour</key><integer>{hour}</integer>
          <key>Minute</key><integer>{minute}</integer>
        </dict>""")


def _install_launchd_fallback(
    jobs: list[tuple[str, dict[str, Any]]],
    *,
    dry_run: bool,
    output_dir: Path | None,
) -> None:
    runner = ROOT / "scripts" / "run_agent_job.sh"
    plist_dir = output_dir or Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)

    for job_id, job in jobs:
        label = f"{LAUNCHD_LABEL_PREFIX}.{job_id}"
        job_id_safe = job_id.replace("-", "_")
        xml = _PLIST_TEMPLATE.format(
            label=label,
            runner=str(runner),
            job_id=job_id,
            cwd=str(ROOT),
            job_id_safe=job_id_safe,
            calendar_xml=_calendar_xml(job),
        )
        dest = plist_dir / f"{label}.plist"
        dest.write_text(xml, encoding="utf-8")
        if dry_run:
            print(f"[dry-run] generated {dest.name}")
        else:
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True, check=False)
            result = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(dest)],
                capture_output=True, text=True, check=False)
            if result.returncode != 0:
                print(f"[!] {job_id} launchd bootstrap 失败: {result.stderr[:200]}",
                      file=sys.stderr)
            else:
                print(f"[+] installed {dest.name}")


# ---------------------------------------------------------------------------
# Unified install
# ---------------------------------------------------------------------------

def install_jobs(
    cfg: dict[str, Any],
    *,
    agent_override: str | None = None,
    dry_run: bool = False,
    output_dir: Path | None = None,
) -> None:
    name = active_agent_name(cfg, agent_override)
    agent = cfg["agents"][name]
    jobs = job_list(cfg)

    cli = agent.get("cli", "")
    if cli and not dry_run and not shutil.which(cli):
        sys.exit(f"错误: 未找到 {cli} 可执行文件。请先安装 {name}。")

    stype = agent["scheduling"]["type"]

    if stype == "config-file":
        fmt = agent["scheduling"]["format"]
        if fmt == "toml":
            _install_config_file_toml(agent, jobs, name,
                                      dry_run=dry_run, output_dir=output_dir)
        elif fmt == "skill-md":
            _install_config_file_skill_md(agent, jobs, name,
                                          dry_run=dry_run, output_dir=output_dir)
    elif stype == "cli-register":
        _install_cli_register(agent, jobs, name, dry_run=dry_run)
    elif stype == "launchd-fallback":
        _install_launchd_fallback(jobs, dry_run=dry_run, output_dir=output_dir)

    print(f"\nInstalled {len(jobs)} automation jobs for agent '{name}' "
          f"(scheduling: {stype})")


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def uninstall_jobs(
    cfg: dict[str, Any],
    *,
    agent_override: str | None = None,
    dry_run: bool = False,
) -> None:
    name = active_agent_name(cfg, agent_override)
    agent = cfg["agents"][name]
    jobs = job_list(cfg)
    stype = agent["scheduling"]["type"]

    if stype == "config-file":
        agent_home = _resolve_agent_home(agent)
        sched = agent["scheduling"]
        for job_id, _ in jobs:
            dest_dir = Path(os.path.expanduser(
                sched["dir"].format(agent_home=agent_home, job_id=job_id)))
            if dest_dir.exists():
                if dry_run:
                    print(f"[dry-run] would remove {dest_dir}")
                else:
                    shutil.rmtree(dest_dir)
                    print(f"[-] removed {dest_dir}")

    elif stype == "cli-register":
        cli = agent["cli"]
        uninstall_tpl = agent["scheduling"].get("uninstall_cmd", "")
        for job_id, _ in jobs:
            cmd_str = f"{cli} {uninstall_tpl}".format(job_id=job_id)
            if dry_run:
                print(f"[dry-run] would run: {cmd_str}")
            else:
                subprocess.run(cmd_str, shell=True, capture_output=True, check=False)
                print(f"[-] unregistered {job_id}")

    elif stype == "launchd-fallback":
        uid = os.getuid()
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        for job_id, _ in jobs:
            label = f"{LAUNCHD_LABEL_PREFIX}.{job_id}"
            plist = plist_dir / f"{label}.plist"
            if dry_run:
                print(f"[dry-run] would remove {label}")
            else:
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{uid}/{label}"],
                    capture_output=True, check=False)
                if plist.exists():
                    plist.unlink()
                print(f"[-] removed {label}")

    print(f"\nUninstalled automation jobs for agent '{name}'")


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify_jobs(
    cfg: dict[str, Any],
    *,
    agent_override: str | None = None,
) -> list[str]:
    name = active_agent_name(cfg, agent_override)
    agent = cfg["agents"][name]
    jobs = job_list(cfg)
    stype = agent["scheduling"]["type"]
    errors: list[str] = []

    cli = agent.get("cli", "")
    if cli and not shutil.which(cli):
        errors.append(f"未找到 {cli} 可执行文件")

    if stype == "config-file":
        agent_home = _resolve_agent_home(agent)
        sched = agent["scheduling"]
        for job_id, _ in jobs:
            dest_dir = Path(os.path.expanduser(
                sched["dir"].format(agent_home=agent_home, job_id=job_id)))
            dest = dest_dir / sched["file"]
            if not dest.exists():
                errors.append(f"配置文件缺失: {dest}")
            elif sched["format"] == "toml":
                text = dest.read_text(encoding="utf-8")
                if str(ROOT) not in text:
                    errors.append(f"{job_id}: cwds 未指向当前项目 {ROOT}")

    elif stype == "cli-register":
        verify_tpl = agent["scheduling"].get("verify_cmd", "")
        if verify_tpl and cli and shutil.which(cli):
            cmd_str = f"{cli} {verify_tpl}"
            result = subprocess.run(
                cmd_str, shell=True, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                errors.append(f"{cli} cron list 失败: {result.stderr[:200]}")
            else:
                for job_id, _ in jobs:
                    if job_id not in result.stdout:
                        errors.append(f"job '{job_id}' 未在 {cli} cron list 中找到")

    elif stype == "launchd-fallback":
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        for job_id, _ in jobs:
            label = f"{LAUNCHD_LABEL_PREFIX}.{job_id}"
            plist = plist_dir / f"{label}.plist"
            if not plist.exists():
                errors.append(f"plist 缺失: {plist}")

    return errors


# ---------------------------------------------------------------------------
# Show
# ---------------------------------------------------------------------------

def show_config(cfg: dict[str, Any], agent_override: str | None = None) -> None:
    name = active_agent_name(cfg, agent_override)
    agent = cfg["agents"][name]
    jobs = job_list(cfg)
    print(f"Active agent: {name}")
    print(f"Scheduling:   {agent['scheduling']['type']}")
    print(f"CLI:          {agent.get('cli', 'N/A')}")
    print(f"Jobs ({len(jobs)}):")
    for job_id, job in jobs:
        print(f"  {job_id:30s}  {job['schedule']['rrule']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-agent automation job manager")
    parser.add_argument("command", choices=["install", "uninstall", "verify", "show"])
    parser.add_argument("--agent", default=None, help="Override agent selection")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Override output directory (config-file / launchd-fallback)")
    parser.add_argument("--replace", action="store_true",
                        help="Uninstall current agent before installing")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to jobs.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    errs = validate(cfg)
    if errs:
        for e in errs:
            print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    if args.command == "show":
        show_config(cfg, args.agent)

    elif args.command == "install":
        if args.replace:
            uninstall_jobs(cfg, agent_override=args.agent, dry_run=args.dry_run)
        install_jobs(cfg, agent_override=args.agent,
                     dry_run=args.dry_run, output_dir=args.output_dir)

    elif args.command == "uninstall":
        uninstall_jobs(cfg, agent_override=args.agent, dry_run=args.dry_run)

    elif args.command == "verify":
        errors = verify_jobs(cfg, agent_override=args.agent)
        if errors:
            for e in errors:
                print(f"FAIL: {e}", file=sys.stderr)
            sys.exit(1)
        else:
            name = active_agent_name(cfg, args.agent)
            print(f"OK: {name} automation 配置检查通过")


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Generic headless agent job runner.
# Called by launchd-fallback plists and manual invocation.
#
# Usage: bash scripts/run_agent_job.sh <job-id>

set -euo pipefail

JOB_ID="${1:?用法: run_agent_job.sh <job-id>}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/anaconda3/bin:$PATH"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
TODAY=$(date +%Y-%m-%d)
LOGFILE="$LOG_DIR/agent_${JOB_ID}_${TODAY}.log"

PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

{
    echo "=========================================="
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] run_agent_job $JOB_ID"
    echo "=========================================="

    # Resolve agent config and execute
    $PYTHON -c "
import sys, os, subprocess, json
sys.path.insert(0, '$ROOT')
from config.jobs_loader import load_config, active_agent_name, active_agent, render_prompt
import shutil

cfg = load_config()
name = active_agent_name(cfg)
profile = active_agent(cfg)

job_id = '$JOB_ID'
if job_id not in cfg['jobs']:
    print(f'错误: 未知 job_id {job_id}', file=sys.stderr)
    sys.exit(1)

job = cfg['jobs'][job_id]
prompt = render_prompt(job, name)
cli = profile.get('cli', 'codex')
cli_path = shutil.which(cli) or cli

exec_cfg = profile.get('exec', {})
cmd_template = exec_cfg.get('cmd', [])
prompt_via = exec_cfg.get('prompt_via', 'stdin')

cmd = [cli_path]
for part in cmd_template:
    cmd.append(part.format(cwd='$ROOT', outfile='/dev/null'))
if prompt_via == 'argument':
    cmd.append(prompt)

print(f'Agent: {name}, CLI: {cli_path}')
print(f'Job: {job_id}, Skill: {job[\"skill\"]}')
print(f'Command: {\" \".join(cmd[:4])}...')

result = subprocess.run(
    cmd,
    cwd='$ROOT',
    input=prompt if prompt_via == 'stdin' else None,
    text=True,
    timeout=600,
    check=False,
)
sys.exit(result.returncode)
"
} >> "$LOGFILE" 2>&1

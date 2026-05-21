#!/usr/bin/env bash
# Install Codex app cron automations for short stock LLM jobs.
# Run this on the machine whose Codex app should execute the jobs.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
AUTOMATIONS_DIR_EXPLICIT=0
if [ "${CODEX_AUTOMATIONS_DIR+x}" ]; then
    AUTOMATIONS_DIR="$CODEX_AUTOMATIONS_DIR"
    AUTOMATIONS_DIR_EXPLICIT=1
else
    AUTOMATIONS_DIR="$CODEX_HOME/automations"
fi
MODEL="${CODEX_AUTOMATION_MODEL:-gpt-5.4}"
REASONING_EFFORT="${CODEX_AUTOMATION_REASONING_EFFORT:-medium}"
DRY_RUN=0

usage() {
    cat <<'USAGE'
Usage: bash scripts/install_codex_automations.sh [--dry-run] [--output-dir DIR]

Options:
  --dry-run         Generate files and print a dry-run summary.
  --output-dir DIR  Write automation files to DIR instead of the configured default.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --output-dir)
            [ "$#" -ge 2 ] || { echo "--output-dir requires a value" >&2; exit 2; }
            AUTOMATIONS_DIR="$2"
            AUTOMATIONS_DIR_EXPLICIT=1
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ ! -d "$ROOT/.agents/skills" ]; then
    echo "Warning: $ROOT/.agents/skills is missing; Codex may not find stock skills." >&2
fi

if [ "$DRY_RUN" -eq 1 ] && [ "$AUTOMATIONS_DIR_EXPLICIT" -eq 0 ]; then
    AUTOMATIONS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/codex-automations.XXXXXX")"
    echo "[dry-run] using temporary automation dir $AUTOMATIONS_DIR"
fi

mkdir -p "$AUTOMATIONS_DIR"

now_ms() {
    python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

skill_prompt() {
    local skill="$1"
    local expected_output="$2"
    local timing="$3"
    cat <<EOF
Use the $skill skill in this repository.

Required behavior:
1. Run the $skill workflow from the current repository checkout.
2. Produce the expected output: $expected_output.
3. Use the unified push.py path for any Telegram delivery required by the skill.
4. Invoke push.py with CARD_VALIDATOR_MODE=enforce. If validation fails, fix the card and retry; do not send a card while validation is only warning.
5. Keep the work unattended; do not ask the user to run commands or provide context that the repository already contains.

Failure handling:
If any required data source, command, validation step, or push.py delivery fails, report the concrete failure and do not claim success. Do not hide partial failures behind a normal summary.

Final response:
Return only a concise operational summary for $timing, including whether files were updated and whether push.py delivered the card.
EOF
}

write_automation() {
    local id="$1"
    local name="$2"
    local rrule="$3"
    local prompt="$4"
    local dir="$AUTOMATIONS_DIR/$id"
    local ts
    ts="$(now_ms)"

    mkdir -p "$dir"
    TOML_ID="$id" \
    TOML_NAME="$name" \
    TOML_RRULE="$rrule" \
    TOML_PROMPT="$prompt" \
    TOML_MODEL="$MODEL" \
    TOML_REASONING_EFFORT="$REASONING_EFFORT" \
    TOML_ROOT="$ROOT" \
    TOML_CREATED_AT="$ts" \
    TOML_UPDATED_AT="$ts" \
    python3 - <<'PY' > "$dir/automation.toml"
import json
import os


def toml_string(name: str) -> str:
    return json.dumps(os.environ[name])


created_at = int(os.environ["TOML_CREATED_AT"])
updated_at = int(os.environ["TOML_UPDATED_AT"])

print("version = 1")
print(f"id = {toml_string('TOML_ID')}")
print('kind = "cron"')
print(f"name = {toml_string('TOML_NAME')}")
print(f"prompt = {toml_string('TOML_PROMPT')}")
print('status = "ACTIVE"')
print(f"rrule = {toml_string('TOML_RRULE')}")
print(f"model = {toml_string('TOML_MODEL')}")
print(f"reasoning_effort = {toml_string('TOML_REASONING_EFFORT')}")
print('execution_environment = "local"')
print(f"cwds = [{toml_string('TOML_ROOT')}]")
print(f"created_at = {created_at}")
print(f"updated_at = {updated_at}")
PY

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] generated $id"
    else
        echo "[+] installed $id"
    fi
}

write_automation \
    "stock-premarket" \
    "stock premarket" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0" \
    "$(skill_prompt "stock-premarket" "today's A-share premarket trading plan, decision_tickets, and Telegram card" "the premarket run")"

write_automation \
    "stock-intraday-09-30" \
    "stock intraday 09:30" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=30" \
    "$(skill_prompt "stock-intraday" "the 09:30 intraday discipline reminder and Telegram card" "the 09:30 intraday run")"

write_automation \
    "stock-intraday-09-45" \
    "stock intraday 09:45" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=45" \
    "$(skill_prompt "stock-intraday" "the 09:45 intraday follow-up and Telegram card" "the 09:45 intraday run")"

write_automation \
    "stock-intraday-11-30" \
    "stock intraday 11:30" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=11;BYMINUTE=30" \
    "$(skill_prompt "stock-intraday" "the midday intraday review and Telegram card" "the 11:30 intraday run")"

write_automation \
    "stock-intraday-14-30" \
    "stock intraday 14:30" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=14;BYMINUTE=30" \
    "$(skill_prompt "stock-intraday" "the 14:30 late-session discipline reminder and Telegram card" "the 14:30 intraday run")"

write_automation \
    "stock-postmarket" \
    "stock postmarket" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=35" \
    "$(skill_prompt "stock-postmarket" "today's A-share postmarket review, tomorrow plan, and Telegram card" "the postmarket run")"

write_automation \
    "stock-weekly-review" \
    "stock weekly review" \
    "FREQ=WEEKLY;INTERVAL=1;BYDAY=SU;BYHOUR=21;BYMINUTE=0" \
    "$(skill_prompt "stock-weekly" "the weekly long-form review and Telegram card" "the weekly review run")"

echo
echo "Installed Codex automations under $AUTOMATIONS_DIR"

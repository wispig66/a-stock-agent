#!/usr/bin/env bash
# Non-destructive runtime readiness checks for Codex stock automations.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"
CODEX_AUTOMATIONS_DIR="$HOME/.codex/automations"
GUI_DOMAIN="gui/$(id -u)"

CODEX_JOBS=(
    "stock-premarket"
    "stock-intraday-09-30"
    "stock-intraday-09-45"
    "stock-intraday-11-30"
    "stock-intraday-14-30"
    "stock-postmarket"
    "stock-weekly-review"
)

SKILLS=(
    "stock-premarket"
    "stock-intraday"
    "stock-postmarket"
    "stock-weekly"
)

LEGACY_LABELS=(
    "com.user.stockpremarket"
    "com.user.stockintraday"
    "com.user.stockpostmarket"
    "com.user.stockweekly"
)

fail() {
    echo "doctor_codex_runtime: $*" >&2
    exit 1
}

warn() {
    echo "WARN: $*" >&2
}

ok() {
    echo "[ok] $*"
}

check_command() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        fail "missing required command: $cmd"
    fi
    ok "command exists: $cmd"
}

check_env_presence() {
    local key="$1"
    local env_file="$PROJECT_ROOT/.env"

    if [ ! -f "$env_file" ]; then
        fail ".env missing; $key is required"
    fi

    if python3 - "$env_file" "$key" <<'PY'
import sys

env_file, key = sys.argv[1], sys.argv[2]

with open(env_file, encoding="utf-8") as fh:
    for raw_line in fh:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1].strip()
        sys.exit(0 if value else 1)

sys.exit(1)
PY
    then
        ok "$key present"
    else
        fail "$key missing or empty in .env"
    fi
}

check_legacy_plist_file_absent() {
    local label="$1"
    local target="$HOME/Library/LaunchAgents/$label.plist"

    if [ -e "$target" ]; then
        fail "legacy short LLM launchd plist still installed: $target"
    fi
    ok "legacy short LLM launchd plist absent: $target"
}

check_automation_cwd() {
    local job="$1"
    local toml="$CODEX_AUTOMATIONS_DIR/$job/automation.toml"

    if [ ! -f "$toml" ]; then
        fail "automation $job missing: $toml"
    fi

    if ! python3 - "$toml" "$PROJECT_ROOT" <<'PY'
import sys

try:
    import tomllib
except ModuleNotFoundError:
    sys.stderr.write("python3 with tomllib is required\n")
    sys.exit(2)

toml_path, project_root = sys.argv[1], sys.argv[2]

with open(toml_path, "rb") as fh:
    data = tomllib.load(fh)

cwds = data.get("cwds")
if not isinstance(cwds, list):
    sys.stderr.write("cwds must be an array\n")
    sys.exit(1)

if not all(isinstance(item, str) for item in cwds):
    sys.stderr.write("cwds must contain only strings\n")
    sys.exit(1)

if project_root not in cwds:
    sys.exit(1)
PY
    then
        fail "automation $job cwd does not point to $PROJECT_ROOT"
    fi

    ok "automation $job cwd ok"
}

legacy_label_loaded() {
    local label="$1"

    if ! command -v launchctl >/dev/null 2>&1; then
        return 1
    fi

    if launchctl print "$GUI_DOMAIN/$label" >/dev/null 2>&1; then
        return 0
    fi

    if launchctl list 2>/dev/null | awk '{print $NF}' | grep -Fxq "$label"; then
        return 0
    fi

    return 1
}

echo "Codex runtime doctor"
echo "PROJECT_ROOT=$PROJECT_ROOT"
echo "CARD_VALIDATOR_MODE=${CARD_VALIDATOR_MODE:-unset}"
echo

if ! command -v uv >/dev/null 2>&1; then
    fail "missing required command: uv"
fi
ok "command exists: uv"

if ! command -v sqlite3 >/dev/null 2>&1; then
    fail "missing required command: sqlite3"
fi
ok "command exists: sqlite3"

[ -d "$PROJECT_ROOT/.agents/skills" ] || fail "missing .agents/skills"
ok ".agents/skills exists"

for skill in "${SKILLS[@]}"; do
    [ -f "$PROJECT_ROOT/.agents/skills/$skill/SKILL.md" ] || fail "missing .agents/skills/$skill/SKILL.md"
    ok "skill $skill exists"
done

check_env_presence TG_BOT_TOKEN
check_env_presence TG_CHAT_ID

[ -f "$PROJECT_ROOT/data/daily.db" ] || fail "missing data/daily.db"
if sqlite3 "$PROJECT_ROOT/data/daily.db" \
    "SELECT name FROM sqlite_master WHERE type='table' AND name='push_log';" | grep -Fxq "push_log"; then
    ok "daily.db push_log table exists"
else
    fail "data/daily.db missing push_log table"
fi

[ -f "$PROJECT_ROOT/data/trade_calendar.csv" ] || fail "missing data/trade_calendar.csv"
ok "data/trade_calendar.csv exists"

for job in "${CODEX_JOBS[@]}"; do
    check_automation_cwd "$job"
done

echo
echo "Loaded stock launchd jobs:"
if command -v launchctl >/dev/null 2>&1; then
    launchctl list 2>/dev/null | grep -E 'com\.user\.stock' || true
else
    warn "launchctl missing; cannot list stock launchd jobs"
fi

for label in "${LEGACY_LABELS[@]}"; do
    if legacy_label_loaded "$label"; then
        fail "legacy short LLM launchd job still loaded: $label"
    fi
    ok "legacy short LLM launchd job not loaded: $label"
    check_legacy_plist_file_absent "$label"
done

echo
echo "Codex runtime doctor complete"

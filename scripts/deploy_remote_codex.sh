#!/usr/bin/env bash
# Pull-based remote deployment for the stock Codex runtime.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_ENV="${DEPLOY_REMOTE_ENV:-$ROOT/deploy.remote.env}"

fail() {
    echo "deploy_remote_codex: $*" >&2
    exit 1
}

quote_double() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\$/\\\$}"
    value="${value//\`/\\\`}"
    printf '"%s"' "$value"
}

if [ ! -f "$DEPLOY_ENV" ]; then
    fail "missing deploy env: $DEPLOY_ENV"
fi

set -a
# shellcheck disable=SC1090
. "$DEPLOY_ENV"
set +a

REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
REMOTE_RUN_TESTS="${REMOTE_RUN_TESTS:-1}"

[ -n "${REMOTE_HOST:-}" ] || fail "REMOTE_HOST is required"
[ -n "${REMOTE_ROOT:-}" ] || fail "REMOTE_ROOT is required"
[ -n "${REMOTE_REPO_URL:-}" ] || fail "REMOTE_REPO_URL is required"

{
    printf 'REMOTE_ROOT=%s\n' "$(quote_double "$REMOTE_ROOT")"
    printf 'REMOTE_REPO_URL=%s\n' "$(quote_double "$REMOTE_REPO_URL")"
    printf 'REMOTE_BRANCH=%s\n' "$(quote_double "$REMOTE_BRANCH")"
    printf 'REMOTE_RUN_TESTS=%s\n' "$(quote_double "$REMOTE_RUN_TESTS")"
    cat <<'REMOTE_PAYLOAD'
set -euo pipefail

if [ ! -d "$REMOTE_ROOT/.git" ]; then
    mkdir -p "$(dirname "$REMOTE_ROOT")"
    git clone "$REMOTE_REPO_URL" "$REMOTE_ROOT"
fi

cd "$REMOTE_ROOT"

git fetch origin "$REMOTE_BRANCH"
git checkout "$REMOTE_BRANCH"
git pull --ff-only origin "$REMOTE_BRANCH"

run_helper() {
    local script="$1"
    bash "$PWD/$script"
}

run_helper scripts/setup.sh # bash scripts/setup.sh
run_helper scripts/sync_codex_skills.sh # bash scripts/sync_codex_skills.sh
run_helper scripts/install_codex_automations.sh # bash scripts/install_codex_automations.sh
run_helper scripts/install_runtime_services.sh # bash scripts/install_runtime_services.sh
run_helper scripts/disable_legacy_claude_launchd.sh # bash scripts/disable_legacy_claude_launchd.sh
run_helper scripts/doctor_codex_runtime.sh # bash scripts/doctor_codex_runtime.sh

if [ "$REMOTE_RUN_TESTS" = "1" ]; then
    uv run pytest tests/
else
    echo "REMOTE_RUN_TESTS=$REMOTE_RUN_TESTS; skip uv run pytest tests/"
fi

echo
echo "Remote deployment summary:"
echo "  root: $REMOTE_ROOT"
echo "  branch: $(git branch --show-current 2>/dev/null || echo "$REMOTE_BRANCH")"
echo "  commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "  tests: $REMOTE_RUN_TESTS"
REMOTE_PAYLOAD
} | ssh "$REMOTE_HOST" "bash -s"

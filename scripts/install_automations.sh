#!/usr/bin/env bash
# Unified multi-agent automation installer.
# Reads config/jobs.yaml and dispatches to the active agent's scheduling path.
#
# Usage:
#   bash scripts/install_automations.sh [--agent <name>] [--dry-run] [--output-dir DIR]
#   bash scripts/install_automations.sh --replace --agent claude-code
#   bash scripts/install_automations.sh --uninstall

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

UV="${UV_BIN:-}"
if [ -z "$UV" ]; then
    for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$HOME/anaconda3/bin/uv"; do
        if [ -x "$candidate" ]; then
            UV="$candidate"
            break
        fi
    done
fi
[ -n "$UV" ] || { echo "error: uv not found" >&2; exit 1; }

PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$UV run --no-sync python"
fi

exec $PYTHON "$ROOT/config/jobs_loader.py" "$@"

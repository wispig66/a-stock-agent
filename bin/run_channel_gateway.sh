#!/usr/bin/env bash
# Run the unified IM gateway in the foreground for launchd.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p data logs

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
        PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3)"
    fi
fi

exec "$PYTHON_BIN" -m stock_codex.apps.channel_listener

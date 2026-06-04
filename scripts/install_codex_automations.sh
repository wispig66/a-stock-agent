#!/usr/bin/env bash
# Legacy wrapper — delegates to the unified multi-agent installer.
# New entry point: scripts/install_automations.sh
#
# Usage unchanged:
#   bash scripts/install_codex_automations.sh [--dry-run] [--output-dir DIR]

set -euo pipefail
exec "$(dirname "$0")/install_automations.sh" install --agent codex "$@"

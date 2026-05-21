#!/usr/bin/env bash
# Validate tracked Codex stock skills.
#
# .agents/skills is the canonical skill tree for this repository. This script is
# kept as an install/runbook compatibility step: it verifies the tree exists.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SKILL_DIR="${CODEX_SKILL_DIR:-$ROOT/.agents/skills}"

if [ ! -d "$SKILL_DIR" ]; then
    echo "error: CODEX_SKILL_DIR does not exist or is not a directory: $SKILL_DIR" >&2
    exit 1
fi

echo "Codex skills ready in $SKILL_DIR:"
find "$SKILL_DIR" -maxdepth 2 -name SKILL.md -print | sort

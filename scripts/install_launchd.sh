#!/usr/bin/env bash
# Legacy wrapper: runtime services now live in install_runtime_services.sh.
# This legacy entrypoint only installs long-running runtime services.
# It no longer installs premarket/intraday/postmarket/weekly LLM jobs;
# those short-running jobs are managed by Codex automations.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "install_launchd.sh is a legacy wrapper."
echo "It only installs long-running runtime services."
echo "Premarket/intraday/postmarket/weekly LLM jobs are managed by Codex automations."
echo "Delegating to install_runtime_services.sh"
exec bash scripts/install_runtime_services.sh "$@"

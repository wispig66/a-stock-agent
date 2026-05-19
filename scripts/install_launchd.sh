#!/usr/bin/env bash
# Legacy wrapper: runtime services now live in install_runtime_services.sh.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "install_launchd.sh is a legacy wrapper; delegating to install_runtime_services.sh"
exec bash scripts/install_runtime_services.sh "$@"

#!/usr/bin/env bash
# Disable the old short LLM launchd jobs after Codex automations are created.
# Long-running watcher daemons are intentionally left alone.

set -euo pipefail

for label in \
    com.user.stockpremarket \
    com.user.stockintraday \
    com.user.stockpostmarket \
    com.user.stockweekly
do
    target="$HOME/Library/LaunchAgents/$label.plist"
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)" "$target"
        echo "[+] bootout $label"
    else
        echo "[-] not loaded $label"
    fi
    if [ -e "$target" ]; then
        rm -f "$target"
        echo "[+] removed $target"
    fi
done

echo
echo "Remaining stock launchd jobs:"
launchctl list | awk '/com.user.stock/ {print "  " $0}'

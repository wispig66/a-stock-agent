#!/usr/bin/env bash
# 安装长时非 LLM runtime 服务。
#
# 用法：从仓库根目录跑 `bash scripts/install_runtime_services.sh`
# 默认只安装长时循环服务；如需同时安装 TG listener：
#   ENABLE_TG_LISTENER_LAUNCHD=1 bash scripts/install_runtime_services.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

TARGET_DIR="$HOME/Library/LaunchAgents"
GUI_DOMAIN="gui/$(id -u)"

install_plist() {
    local template="$1"
    local plist_name
    local target
    local label

    if [ ! -f "$template" ]; then
        echo "missing plist template: $template" >&2
        exit 1
    fi

    plist_name="$(basename "$template")"
    target="$TARGET_DIR/$plist_name"
    label="${plist_name%.plist}"

    if launchctl print "$GUI_DOMAIN/$label" >/dev/null 2>&1; then
        launchctl bootout "$GUI_DOMAIN" "$target" 2>/dev/null || true
        echo "[+] bootout $label"
    fi

    sed "s|{{PROJECT_ROOT}}|$PROJECT_ROOT|g" "$template" > "$target"
    launchctl bootstrap "$GUI_DOMAIN" "$target"
    echo "[+] bootstrap $label"
}

mkdir -p "$TARGET_DIR" logs

install_plist "launchd/com.user.stockwatchloop.plist"
install_plist "launchd/com.user.stockanomalyloop.plist"
install_plist "launchd/com.user.stockthemeloop.plist"

if [ "${ENABLE_TG_LISTENER_LAUNCHD:-0}" = "1" ]; then
    install_plist "launchd/disabled/com.user.stocktglistener.plist"
fi

echo
echo "runtime services installed"

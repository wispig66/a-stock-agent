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
TEMPLATES=(
    "launchd/com.user.stockwatchloop.plist"
    "launchd/com.user.stockanomalyloop.plist"
    "launchd/com.user.stockthemeloop.plist"
)

if [ "${ENABLE_TG_LISTENER_LAUNCHD:-0}" = "1" ]; then
    TEMPLATES+=("launchd/disabled/com.user.stocktglistener.plist")
fi

RENDERED_DIR=""

preflight() {
    local template

    if ! command -v python3 >/dev/null 2>&1; then
        echo "missing required command: python3" >&2
        exit 1
    fi

    if ! command -v plutil >/dev/null 2>&1; then
        echo "missing required command: plutil" >&2
        exit 1
    fi

    for template in "${TEMPLATES[@]}"; do
        if [ ! -f "$template" ]; then
            echo "missing plist template: $template" >&2
            exit 1
        fi
    done
}

render_plist() {
    local template="$1"
    local rendered_dir="$2"
    local plist_name

    plist_name="$(basename "$template")"
    python3 - "$template" "$rendered_dir/$plist_name" "$PROJECT_ROOT" "$HOME" <<'PY'
from pathlib import Path
import sys
from xml.sax.saxutils import escape

template_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
project_root = sys.argv[3]
home = sys.argv[4]

text = template_path.read_text(encoding="utf-8")
for marker, value in {
    "{{PROJECT_ROOT}}": project_root,
    "{{HOME}}": home,
}.items():
    text = text.replace(marker, escape(value))
target_path.write_text(text, encoding="utf-8")
PY
}

unload_if_loaded() {
    local label="$1"
    local target="$2"

    if ! launchctl print "$GUI_DOMAIN/$label" >/dev/null 2>&1; then
        return 0
    fi

    if launchctl bootout "$GUI_DOMAIN/$label" >/dev/null 2>&1; then
        echo "[+] bootout $label"
        return 0
    fi

    if launchctl bootout "$GUI_DOMAIN" "$target"; then
        echo "[+] bootout $label"
        return 0
    fi

    echo "failed to bootout loaded service: $label" >&2
    exit 1
}

install_plist() {
    local template="$1"
    local rendered_dir="$2"
    local plist_name
    local target
    local label

    plist_name="$(basename "$template")"
    target="$TARGET_DIR/$plist_name"
    label="${plist_name%.plist}"

    unload_if_loaded "$label" "$target"

    cp "$rendered_dir/$plist_name" "$target"
    launchctl bootstrap "$GUI_DOMAIN" "$target"
    echo "[+] bootstrap $label"
}

preflight
RENDERED_DIR="$(mktemp -d "${TMPDIR:-/tmp}/stock-runtime-services.XXXXXX")"
trap 'rm -rf "$RENDERED_DIR"' EXIT

for template in "${TEMPLATES[@]}"; do
    render_plist "$template" "$RENDERED_DIR"
done

for template in "${TEMPLATES[@]}"; do
    plist_name="$(basename "$template")"
    plutil -lint "$RENDERED_DIR/$plist_name" >/dev/null
done

if ! command -v launchctl >/dev/null 2>&1; then
    echo "missing required command: launchctl" >&2
    exit 1
fi

mkdir -p "$TARGET_DIR" logs

for template in "${TEMPLATES[@]}"; do
    install_plist "$template" "$RENDERED_DIR"
done

echo
echo "runtime services installed"

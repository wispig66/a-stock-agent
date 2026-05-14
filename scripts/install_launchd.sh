#!/usr/bin/env bash
# 单独重装 launchd 任务（plist 模板有改动 / 路径变更后）
# 用法：从仓库根目录跑 `bash scripts/install_launchd.sh`
# 幂等：会先 bootout 已存在的同名任务再 bootstrap。

set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCHD_DIR"

for plist_template in launchd/com.user.stock*.plist; do
    [ -f "$plist_template" ] || continue
    plist_name=$(basename "$plist_template")
    target="$LAUNCHD_DIR/$plist_name"
    label="${plist_name%.plist}"

    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)" "$target" 2>/dev/null || true
        echo "[+] bootout $label"
    fi

    sed "s|{{PROJECT_ROOT}}|$PROJECT_ROOT|g" "$plist_template" > "$target"
    launchctl bootstrap "gui/$(id -u)" "$target"
    echo "[+] bootstrap $label"
done

echo
echo "已安装的 stock 任务："
launchctl list | awk '/com.user.stock/ {print "  " $0}'

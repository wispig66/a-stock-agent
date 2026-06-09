#!/usr/bin/env bash
# Install/restart the persistent unified IM gateway launchd agent.
# The agent runs channel_listener in the foreground and launchd keeps it alive.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs
LOG="logs/launchd_channelgateway_stderr.log"
LABEL="com.user.stockchannelgateway"
PLIST_NAME="com.user.stockchannelgateway.plist"
TEMPLATE="$PROJECT_ROOT/launchd/$PLIST_NAME"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET="$TARGET_DIR/$PLIST_NAME"
GUI_DOMAIN="gui/$(id -u)"

render_plist() {
  local output="$1"
  python3 - "$TEMPLATE" "$output" "$PROJECT_ROOT" "$HOME" <<'PY'
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

if command -v launchctl >/dev/null 2>&1 && [ "$(uname -s)" = "Darwin" ]; then
  if [ ! -f "$TEMPLATE" ]; then
    echo "[-] missing launchd template: $TEMPLATE" >&2
    exit 1
  fi
  RENDERED="$(mktemp "${TMPDIR:-/tmp}/stockchannelgateway.XXXXXX.plist")"
  trap 'rm -f "$RENDERED"' EXIT
  render_plist "$RENDERED"
  plutil -lint "$RENDERED" >/dev/null

  mkdir -p "$TARGET_DIR"
  launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || launchctl remove "$LABEL" 2>/dev/null || true
  cp "$RENDERED" "$TARGET"
  launchctl bootstrap "$GUI_DOMAIN" "$TARGET"
  launchctl kickstart -k "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
  sleep 2
  if launchctl print "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "[+] IM gateway installed and started via launchd label=$LABEL log=$LOG"
    exit 0
  fi
  echo "[-] launchctl failed to start persistent IM gateway; recent log:" >&2
  tail -n 40 "$LOG" >&2 || true
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

LOG="logs/channel_listener.log"
nohup "$PYTHON_BIN" -m stock_codex.apps.channel_listener >>"$LOG" 2>&1 &
PID=$!
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  echo "[-] IM gateway failed to stay running; recent log:" >&2
  tail -n 40 "$LOG" >&2 || true
  exit 1
fi
echo "[+] IM gateway started pid=$PID log=$LOG"

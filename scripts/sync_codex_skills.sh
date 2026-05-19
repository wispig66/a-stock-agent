#!/usr/bin/env bash
# Build Codex-local stock skills from the canonical .claude/skills tree.
# .agents/ is gitignored, so run this on each machine that should use Codex
# skills or Codex automations.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SRC_DIR="${CODEX_SKILL_SOURCE_DIR:-$ROOT/.claude/skills}"
DST_DIR="${CODEX_SKILL_DEST_DIR:-$ROOT/.agents/skills}"

canonical_path() {
    python3 -c 'import os, sys; print(os.path.realpath(os.path.abspath(sys.argv[1])))' "$1"
}

SRC_REAL="$(canonical_path "$SRC_DIR")"
DST_REAL="$(canonical_path "$DST_DIR")"

if [ "$SRC_REAL" = "$DST_REAL" ] || [[ "$DST_REAL/" == "$SRC_REAL/"* ]] || [[ "$SRC_REAL/" == "$DST_REAL/"* ]]; then
    echo "error: CODEX_SKILL_SOURCE_DIR and CODEX_SKILL_DEST_DIR must not contain each other: source=$SRC_REAL destination=$DST_REAL" >&2
    exit 1
fi

if [ ! -d "$SRC_DIR" ]; then
    echo "error: CODEX_SKILL_SOURCE_DIR does not exist or is not a directory: $SRC_DIR" >&2
    exit 1
fi

mkdir -p "$DST_DIR"

for src in "$SRC_DIR"/stock-*; do
    [ -d "$src" ] || continue
    name="$(basename "$src")"
    dst="$DST_DIR/$name"
    rm -rf "$dst"
    cp -R "$src" "$dst"
done

tmp_files="$(mktemp)"
trap 'rm -f "$tmp_files"' EXIT

if command -v rg >/dev/null 2>&1; then
    rg --no-ignore -l '\.claude/skills|\.Codex/skills|\.claude -> stock' "$DST_DIR"/stock-* > "$tmp_files" 2>/dev/null || true
else
    grep -RIlE '\.claude/skills|\.Codex/skills|\.claude -> stock' "$DST_DIR"/stock-* > "$tmp_files" 2>/dev/null || true
fi

if [ -s "$tmp_files" ]; then
    while IFS= read -r file; do
        perl -pi -e 's#\.Codex/skills#.agents/skills#g; s#\.claude/skills#.agents/skills#g; s#\.claude -> stock#.agents -> stock#g' "$file"
    done < "$tmp_files"
fi

echo "Synced Codex skills into $DST_DIR:"
find "$DST_DIR" -maxdepth 2 -name SKILL.md -print | sort

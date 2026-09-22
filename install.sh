#!/usr/bin/env bash
# Install or update this skill into Claude Code's personal skills directory.
# The installed copy is a plain file copy, so it goes stale silently: re-run this after
# every `git pull`. `python3 ~/.claude/skills/seo-audit/scripts/seo_audit.py --version`
# shows what is installed.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/.claude/skills/seo-audit}"
mkdir -p "$DEST"
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude 'tests' --exclude 'install.sh' \
  "$SRC/SKILL.md" "$SRC/scripts" "$SRC/references" "$DEST/"
git -C "$SRC" rev-parse --short HEAD > "$DEST/.installed-from" 2>/dev/null || true
echo "Installed $(python3 "$DEST/scripts/seo_audit.py" --version) from $(cat "$DEST/.installed-from" 2>/dev/null || echo '?') to $DEST"

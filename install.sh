#!/usr/bin/env bash
#
# install.sh — register Kstack skills with Claude Code.
#
# Default: symlink each folder under skills/ into ~/.claude/skills/ so that
# edits in this repo are reflected immediately.
#
# Use --copy to copy instead of symlink (useful if ~/.claude/skills/ is on a
# different volume or you want to pin a version).
#
set -euo pipefail

MODE="link"
if [ "${1:-}" = "--copy" ]; then
    MODE="copy"
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_SRC="$REPO_DIR/skills"
SKILLS_DST="$HOME/.claude/skills"

if [ ! -d "$SKILLS_SRC" ]; then
    echo "ERROR: $SKILLS_SRC not found" >&2
    exit 1
fi

mkdir -p "$SKILLS_DST"

echo "Installing Kstack skills into $SKILLS_DST (mode: $MODE)"
echo ""

for skill_dir in "$SKILLS_SRC"/*/; do
    name="$(basename "$skill_dir")"
    target="$SKILLS_DST/$name"

    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ -L "$target" ] && [ "$(readlink "$target")" = "$skill_dir" ]; then
            echo "  [skip]    $name (already linked)"
            continue
        fi
        echo "  [backup]  $target -> $target.bak"
        rm -rf "$target.bak"
        mv "$target" "$target.bak"
    fi

    if [ "$MODE" = "link" ]; then
        ln -s "$skill_dir" "$target"
        echo "  [link]    $name"
    else
        cp -r "$skill_dir" "$target"
        echo "  [copy]    $name"
    fi
done

echo ""
echo "Done. Verify with:"
echo "  ls -la $SKILLS_DST | grep -E 'kicad|browse|gstack'"
echo ""
echo "Make sure the 'kicad-agent' conda env has kiutils + PyYAML:"
echo "  conda create -n kicad-agent python=3.11 -y && \\"
echo "      conda activate kicad-agent && \\"
echo "      pip install kiutils pyyaml"

#!/usr/bin/env bash
# install.sh — install the `nopper` CLI + the `nopper-setup` project skill.
#
# This installer does NOT touch ~/.claude/settings.json and does NOT register
# any global hooks. The fact-graph is strictly per-project opt-in: you opt a
# project in by running `nopper setup` (or the /nopper-setup skill) inside that
# project, which writes ./.claude/settings.json + .nopper/ there.
#
# Usage:
#   bash install.sh              # install CLI + project skill (recommended)
#   bash install.sh --cli-only   # just symlink nopper, skip the skill
#   bash install.sh --uninstall  # remove the symlink + the skill
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/lib/nopper.py"
mode="${1:-install}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "install: python3 is required (stdlib only, no pip)." >&2
  exit 1
fi

# --- uninstall -------------------------------------------------------------
if [[ "$mode" == "--uninstall" ]]; then
  rm -f "${HOME}/.local/bin/nopper"
  echo "removed ~/.local/bin/nopper"
  if [[ -e "${HOME}/.claude/skills/nopper-setup" || -L "${HOME}/.claude/skills/nopper-setup" ]]; then
    rm -f "${HOME}/.claude/skills/nopper-setup"
    echo "removed ~/.claude/skills/nopper-setup"
  fi
  echo "uninstalled."
  echo "(Project-level .nopper/ graphs and ./.claude/settings.json in your projects are untouched."
  echo " Remove them per-project with: cd <project> && nopper teardown --purge)"
  exit 0
fi

# --- install CLI -----------------------------------------------------------
if [[ ! -f "$SRC" ]]; then
  echo "install: nopper.py not found at $SRC" >&2; exit 1
fi

DEST_DIR="${HOME}/.local/bin"
mkdir -p "$DEST_DIR"
if [[ -e "$DEST_DIR/nopper" || -L "$DEST_DIR/nopper" ]]; then
  rm -f "$DEST_DIR/nopper"
fi
ln -s "$SRC" "$DEST_DIR/nopper"
chmod +x "$SRC"
echo "installed: nopper -> $DEST_DIR/nopper"

case ":$PATH:" in
  *":$DEST_DIR:"*) ;;
  *) echo "install: NOTE — $DEST_DIR is not on your PATH. Add:" >&2
     echo "    export PATH=\"$DEST_DIR:\$PATH\"" >&2 ;;
esac

"$DEST_DIR/nopper" --version >/dev/null && echo "  (nopper works)"

if [[ "$mode" == "--cli-only" ]]; then
  echo "(--cli-only: skipping the /nopper-setup skill)"
  exit 0
fi

# --- install the project skill --------------------------------------------
SKILLS_DIR="${HOME}/.claude/skills"
mkdir -p "$SKILLS_DIR"
SKILL_LINK="$SKILLS_DIR/nopper-setup"
if [[ -e "$SKILL_LINK" || -L "$SKILL_LINK" ]]; then
  rm -f "$SKILL_LINK"
fi
ln -s "${SCRIPT_DIR}/skill" "$SKILL_LINK"
echo "installed: /nopper-setup skill -> $SKILL_LINK"

cat <<'NOTE'

Done. Nothing global was configured — the fact-graph is per-project opt-in.

To opt a project IN (run inside the project dir):
  nopper setup --goal "your one-line goal" --agents
  #  ↑ creates ./.nopper/  +  registers hooks in ./.claude/settings.json  +  AGENTS.md

Or just invoke the skill from inside Claude Code:
  /nopper-setup <your goal>

After that, every turn in that project auto-injects the current graph state,
and you can drive it with `nopper dispatch reason` / `nopper auto`.

To opt a project OUT:
  cd <project> && nopper teardown           # removes hooks + AGENTS.md (keeps .nopper/)
  cd <project> && nopper teardown --purge   # also deletes the graph data
NOTE

#!/usr/bin/env bash
# install.sh: copy kanban-lanes into your Claude Code config.
#
#   ./install.sh              install or update (safe to run again)
#   ./install.sh --dry-run    print what it would do, change nothing
#   ./install.sh --uninstall  remove what it installed (add --dry-run to preview)
#
# Where things go:
#   agents/kanban-watcher.md     -> $CLAUDE_CONFIG_DIR/agents/          (default ~/.claude)
#   workflows/kanban-watch.js    -> $CLAUDE_CONFIG_DIR/workflows/
#   skills/dynamic-workflow/     -> $CLAUDE_CONFIG_DIR/skills/dynamic-workflow/
#   scripts/, hooks/             -> $KANBAN_LANES_HOME/  (default ~/.claude/kanban-lanes)
#   examples/, docs/             -> $KANBAN_LANES_HOME/  (the template and guide the skill names)
#
# If a destination is this checkout itself (for example KANBAN_LANES_HOME points here), that
# part is left alone and reported as already in place.
#
# It never edits settings.json. It prints the snippet for the optional hook instead.
# A file or folder of yours that is in the way is moved aside to <name>.bak first.
set -euo pipefail

usage() {
  sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
}

DRY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown option '$arg'" >&2; usage >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
LANES="${KANBAN_LANES_HOME:-$HOME/.claude/kanban-lanes}"
MARK=".installed-by-kanban-lanes"

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
# done_msg VERB PATH: "installed: PATH", or "would be installed: PATH" under --dry-run.
done_msg() { if [ "$DRY" -eq 1 ]; then say "would be $1: $2"; else say "$1: $2"; fi; }

# run CMD...: run it, or print it under --dry-run.
run() {
  if [ "$DRY" -eq 1 ]; then
    printf 'would run:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# resolve PATH: print PATH with symlinks resolved. PATH need not exist; its nearest existing
# parent is resolved and the rest is appended.
resolve() {
  local p="$1" rest=""
  while [ ! -e "$p" ] && [ "$p" != "/" ] && [ "$p" != "." ]; do
    rest="/$(basename -- "$p")$rest"
    p="$(dirname -- "$p")"
  done
  if [ -d "$p" ]; then
    printf '%s%s\n' "$(cd -- "$p" && pwd -P)" "$rest"
  else
    printf '%s%s\n' "$(cd -- "$(dirname -- "$p")" && pwd -P)/$(basename -- "$p")" "$rest"
  fi
}

# in_checkout DST: true when DST is this checkout or a path inside it. Installing or removing
# there would move or delete the repo's own files, so callers skip it.
in_checkout() {
  local d
  d="$(resolve "$1")"
  [ "$d" = "$REPO" ] || [ "${d#"$REPO"/}" != "$d" ]
}

# backup PATH: move PATH aside to PATH.bak (or PATH.bak.N if that is taken).
backup() {
  local path="$1" dest="$1.bak" n=1
  while [ -e "$dest" ]; do dest="$1.bak.$n"; n=$((n + 1)); done
  say "moving your existing $path to $dest"
  run mv -- "$path" "$dest"
}

# install_file SRC DST: copy one file; skip when SRC is missing or DST is already identical.
install_file() {
  local src="$1" dst="$2"
  if [ ! -f "$src" ]; then
    warn "${src#"$REPO"/} is not in this checkout yet; skipped"
    return 0
  fi
  if in_checkout "$dst"; then
    say "already in place (inside this checkout): $dst"
    return 0
  fi
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    say "up to date: $dst"
    return 0
  fi
  [ -e "$dst" ] && backup "$dst"
  run mkdir -p -- "$(dirname "$dst")"
  run cp -- "$src" "$dst"
  done_msg installed "$dst"
}

# install_dir SRC DST: replace DST with a copy of SRC, marked as ours.
install_dir() {
  local src="$1" dst="$2"
  if [ ! -d "$src" ]; then
    warn "${src#"$REPO"/}/ is not in this checkout yet; skipped"
    return 0
  fi
  if in_checkout "$dst"; then
    say "already in place (inside this checkout): $dst/"
    return 0
  fi
  if [ -e "$dst" ] && [ ! -f "$dst/$MARK" ]; then
    backup "$dst"
  elif [ -e "$dst" ]; then
    run rm -rf -- "$dst"
  fi
  run mkdir -p -- "$(dirname "$dst")"
  run cp -R -- "$src" "$dst"
  if [ "$DRY" -eq 0 ]; then
    find "$dst" -name __pycache__ -type d -prune -exec rm -rf {} +
    printf 'Installed by kanban-lanes from %s. install.sh --uninstall removes this folder.\n' "$REPO" > "$dst/$MARK"
  fi
  done_msg installed "$dst/"
}

# remove_file SRC DST: remove DST only when it still matches the repo copy.
remove_file() {
  local src="$1" dst="$2"
  [ -e "$dst" ] || return 0
  if in_checkout "$dst"; then
    say "left in place (inside this checkout): $dst"
    return 0
  fi
  if [ -f "$src" ] && cmp -s "$src" "$dst"; then
    run rm -f -- "$dst"
    done_msg removed "$dst"
  else
    warn "$dst differs from this checkout (or the source is missing); left in place"
  fi
}

# remove_dir DST: remove DST only when it carries our marker.
remove_dir() {
  local dst="$1"
  [ -e "$dst" ] || return 0
  if in_checkout "$dst"; then
    say "left in place (inside this checkout): $dst/"
    return 0
  fi
  if [ -f "$dst/$MARK" ]; then
    run rm -rf -- "$dst"
    done_msg removed "$dst/"
  else
    warn "$dst was not installed by kanban-lanes; left in place"
  fi
}

check_tools() {
  if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
      warn "python3 is $(python3 -c 'import platform; print(platform.python_version())'); the scripts need 3.10 or newer"
    fi
  else
    warn "python3 not found; the scripts and the hook need Python 3.10 or newer"
  fi
  if ! command -v hermes >/dev/null 2>&1; then
    warn "hermes is not on PATH; kanban-lanes needs Hermes Agent ('hermes kanban') to file and run cards"
  fi
  if ! command -v node >/dev/null 2>&1; then
    warn "node not found; the linter cannot run its parse check (rule0) and will say UNKNOWN"
  fi
}

print_hook_snippet() {
  local hook="$LANES/hooks/kanban-watch-nudge.py"
  cat <<EOF

Optional hook: a reminder to launch the watcher after 'hermes kanban create' or 'swarm'.
install.sh does not edit settings.json. To turn it on, add this to $CFG/settings.json
(merge it into an existing "hooks" block if you have one):

{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "python3 \"$hook\"" }
        ]
      }
    ]
  }
}
EOF
}

[ "$DRY" -eq 1 ] && say "dry run: nothing will change"

if [ "$UNINSTALL" -eq 1 ]; then
  remove_file "$REPO/agents/kanban-watcher.md" "$CFG/agents/kanban-watcher.md"
  remove_file "$REPO/workflows/kanban-watch.js" "$CFG/workflows/kanban-watch.js"
  remove_dir "$CFG/skills/dynamic-workflow"
  remove_dir "$LANES/scripts"
  remove_dir "$LANES/hooks"
  remove_dir "$LANES/examples"
  remove_dir "$LANES/docs"
  if [ -d "$LANES" ] && ! in_checkout "$LANES" && [ -z "$(ls -A "$LANES")" ]; then
    run rmdir -- "$LANES"
    done_msg removed "$LANES/"
  fi
  say "If you added the hook to $CFG/settings.json, remove that entry yourself."
  say "Backups (*.bak) from earlier installs are left in place."
  [ "$DRY" -eq 1 ] && say "Dry run finished; nothing changed."
  exit 0
fi

check_tools
install_file "$REPO/agents/kanban-watcher.md" "$CFG/agents/kanban-watcher.md"
install_file "$REPO/workflows/kanban-watch.js" "$CFG/workflows/kanban-watch.js"
install_dir "$REPO/skills/dynamic-workflow" "$CFG/skills/dynamic-workflow"
install_dir "$REPO/scripts" "$LANES/scripts"
install_dir "$REPO/hooks" "$LANES/hooks"
install_dir "$REPO/examples" "$LANES/examples"
install_dir "$REPO/docs" "$LANES/docs"

if [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ -z "${KANBAN_LANES_HOME:-}" ]; then
  say "note: scripts went to $LANES. The watcher finds them there unless you set KANBAN_LANES_HOME."
fi
print_hook_snippet
say ""
if [ "$DRY" -eq 1 ]; then
  say "Dry run finished; nothing changed."
else
  say "Done. Start a new Claude Code session so it picks up the agent, skill and workflow."
fi

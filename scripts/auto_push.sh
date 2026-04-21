#!/usr/bin/env bash
set -euo pipefail

# Auto-commit + push on file changes (macOS/Linux)
#
# macOS: requires fswatch (recommended)
#   brew install fswatch
#
# Linux: supports inotifywait if installed:
#   sudo apt-get install inotify-tools
#
# Usage:
#   ./scripts/auto_push.sh
#   ./scripts/auto_push.sh --message-prefix "wip"
#   ./scripts/auto_push.sh --branch master --remote origin
#
# Notes:
# - This will create MANY commits. Use a feature branch if you care about history.
# - Ensure your .gitignore excludes secrets (.env, credentials, etc).

REMOTE="origin"
BRANCH=""
MESSAGE_PREFIX="auto"
DEBOUNCE_SECONDS="2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote) REMOTE="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --message-prefix) MESSAGE_PREFIX="${2:-}"; shift 2 ;;
    --debounce-seconds) DEBOUNCE_SECONDS="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '1,120p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not a git repo: $ROOT" >&2
  exit 1
fi

if [[ -z "$BRANCH" ]]; then
  BRANCH="$(git branch --show-current 2>/dev/null || true)"
fi

echo "Watching for changes in: $ROOT"
echo "Auto push to: ${REMOTE}/${BRANCH:-<current>}"
echo "Debounce: ${DEBOUNCE_SECONDS}s"

_commit_and_push() {
  # Avoid committing during merges/rebases.
  if [[ -f ".git/MERGE_HEAD" || -d ".git/rebase-apply" || -d ".git/rebase-merge" ]]; then
    echo "[skip] git operation in progress (merge/rebase)"
    return 0
  fi

  git add -A

  # Nothing to commit.
  if git diff --cached --quiet; then
    return 0
  fi

  local ts msg
  ts="$(date +"%Y-%m-%d %H:%M:%S")"
  msg="${MESSAGE_PREFIX}: ${ts}"

  git commit -m "$msg" >/dev/null

  if [[ -n "$BRANCH" ]]; then
    git push "$REMOTE" "$BRANCH"
  else
    git push "$REMOTE" HEAD
  fi
}

if command -v fswatch >/dev/null 2>&1; then
  # fswatch emits bursts; -o collapses to "something changed" events.
  # Exclude common heavy dirs. (gitignore still controls what gets committed.)
  fswatch -o \
    --exclude '(\.git/|node_modules/|\.venv/|venv/|__pycache__/|ui/dist/|ui/src-tauri/target/|cache/)' \
    --include '.*' \
    "$ROOT" | while read -r _; do
      sleep "$DEBOUNCE_SECONDS"
      _commit_and_push || true
    done
elif command -v inotifywait >/dev/null 2>&1; then
  # Linux fallback
  while inotifywait -r -q \
    -e close_write,move,create,delete \
    --exclude '(\.git/|node_modules/|\.venv/|venv/|__pycache__/|ui/dist/|ui/src-tauri/target/|cache/)' \
    "$ROOT"; do
    sleep "$DEBOUNCE_SECONDS"
    _commit_and_push || true
  done
else
  echo "Need fswatch (macOS) or inotifywait (Linux)." >&2
  echo "macOS: brew install fswatch" >&2
  echo "Linux: sudo apt-get install inotify-tools" >&2
  exit 1
fi


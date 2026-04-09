#!/usr/bin/env bash
set -euo pipefail

# Export a portable bundle containing:
# - the project (excluding heavy artifacts)
# - runtime state (FirmState snapshot + XAI logs)
# - Cursor project knowledge (agent transcripts + project metadata), if present

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${PROJECT_ROOT}/transfer_bundle"

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}/project" "${OUT_DIR}/runtime_state" "${OUT_DIR}/cursor_project"

echo "[1/4] Copying project (excluding build artifacts)…"

# Prefer rsync if available for exclusions; fall back to tar.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude ".venv/" \
    --exclude "**/__pycache__/" \
    --exclude "**/*.pyc" \
    --exclude "ui/node_modules/" \
    --exclude "ui/dist/" \
    --exclude "logs/**" \
    --exclude "transfer_bundle/" \
    "${PROJECT_ROOT}/" "${OUT_DIR}/project/"
else
  # tar-based copy with exclusions
  tar -C "${PROJECT_ROOT}" -cf - \
    --exclude=".venv" \
    --exclude="**/__pycache__" \
    --exclude="**/*.pyc" \
    --exclude="ui/node_modules" \
    --exclude="ui/dist" \
    --exclude="logs" \
    --exclude="transfer_bundle" \
    . | tar -C "${OUT_DIR}/project" -xf -
fi

echo "[2/4] Copying runtime state (FirmState + XAI logs)…"
mkdir -p "${OUT_DIR}/runtime_state/agents" "${OUT_DIR}/runtime_state/logs"

if [[ -f "${PROJECT_ROOT}/agents/_firm_state.json" ]]; then
  cp "${PROJECT_ROOT}/agents/_firm_state.json" "${OUT_DIR}/runtime_state/agents/_firm_state.json"
else
  echo "  - No agents/_firm_state.json found (ok)."
fi

if [[ -d "${PROJECT_ROOT}/logs/xai" ]]; then
  mkdir -p "${OUT_DIR}/runtime_state/logs/xai"
  cp -R "${PROJECT_ROOT}/logs/xai/." "${OUT_DIR}/runtime_state/logs/xai/" || true
else
  echo "  - No logs/xai directory found (ok)."
fi

echo "[3/4] Copying Cursor project knowledge (best effort)…"

# Cursor stores per-project data under ~/.cursor/projects/<project-id>/
# We try to locate the project directory by matching the current project folder name.
CURSOR_PROJECTS_DIR="${HOME}/.cursor/projects"
if [[ -d "${CURSOR_PROJECTS_DIR}" ]]; then
  # Heuristic: copy any Cursor project folder that contains this project name in its directory name.
  # (Your current machine uses: Users-bhargavchirumamilla-Downloads-Trading-Bot)
  MATCHES=()
  while IFS= read -r -d '' p; do MATCHES+=("$p"); done < <(find "${CURSOR_PROJECTS_DIR}" -maxdepth 1 -type d -name "*Trading-Bot*" -print0 2>/dev/null || true)

  if [[ ${#MATCHES[@]} -gt 0 ]]; then
    # Copy all matches (safe if you renamed paths across time)
    for m in "${MATCHES[@]}"; do
      base="$(basename "$m")"
      mkdir -p "${OUT_DIR}/cursor_project/${base}"
      cp -R "$m/." "${OUT_DIR}/cursor_project/${base}/" || true
    done
  else
    echo "  - No matching Cursor project folder found under ~/.cursor/projects (ok)."
  fi
else
  echo "  - ~/.cursor/projects not found (ok)."
fi

echo "[4/4] Done."
echo "Bundle created at: ${OUT_DIR}"
echo "Next: zip transfer_bundle and copy to new laptop."


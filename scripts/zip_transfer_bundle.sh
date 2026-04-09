#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -d "${PROJECT_ROOT}/transfer_bundle" ]]; then
  echo "transfer_bundle not found. Run: bash scripts/export_cursor_bundle.sh"
  exit 1
fi

OUT_ZIP="${PROJECT_ROOT}/transfer_bundle.zip"
rm -f "${OUT_ZIP}"

echo "Creating ${OUT_ZIP} (this can take a bit)…"
cd "${PROJECT_ROOT}"
zip -r "${OUT_ZIP}" transfer_bundle >/dev/null
echo "Done: ${OUT_ZIP}"


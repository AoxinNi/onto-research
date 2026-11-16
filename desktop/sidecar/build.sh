#!/usr/bin/env bash
# Build the bundled local workbench: Python, the server and an embedded PostgreSQL 16 (pgserver) frozen into one
# executable, placed where Tauri's externalBin expects it (src-tauri/binaries/onto-workbench-<target triple>).
#
#   desktop/sidecar/build.sh            # needs uv and rustc on PATH
#   cd desktop && npm run build:local   # this, then `tauri build` with the sidecar bundled
#
# Build on each platform you ship for; PyInstaller does not cross-compile.
set -euo pipefail
cd "$(dirname "$0")/../.."

TRIPLE="${TAURI_TARGET_TRIPLE:-$(rustc -vV | sed -n 's/^host: //p')}"
OUT=desktop/src-tauri/binaries
WORK=build/sidecar
EXT=""
case "$TRIPLE" in *windows*) EXT=".exe" ;; esac

uv venv --quiet --clear --python 3.11 "$WORK/venv"
uv pip install --quiet --python "$WORK/venv/bin/python" ".[local]" "pyinstaller>=6.10"

# What goes in, and the one libpq both pgserver and psycopg load, is in the spec.
"$WORK/venv/bin/python" -m PyInstaller --noconfirm --clean \
  --distpath "$WORK/dist" --workpath "$WORK/work" desktop/sidecar/onto-workbench.spec

mkdir -p "$OUT"
cp "$WORK/dist/onto-workbench$EXT" "$OUT/onto-workbench-$TRIPLE$EXT"
echo "built $OUT/onto-workbench-$TRIPLE$EXT ($(du -h "$OUT/onto-workbench-$TRIPLE$EXT" | cut -f1))"

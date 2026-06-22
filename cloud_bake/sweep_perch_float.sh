#!/usr/bin/env bash
# Parallel perch + floating-tree sweep over the installed S94c tiles.
set -u
DEST="${1:-/d/modrinth_vandir/saves/Vandir50k_verify/region}"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
cd C:/Users/nicho/minecraft-worldgen || exit 2
TILES="51,52 52,52 53,52 51,53 52,53 53,53 51,54 52,54 53,54 69,61 69,62 69,63 89,57 89,58 12,80 13,80 16,76 60,72 20,74 20,63 12,82 65,81 35,21 67,54 79,71 73,65 74,65 75,65 73,66 74,66 75,66 73,67 74,67 75,67 73,68 74,68 72,69 33,25 42,29 34,24 55,45"
one(){
  local t="$1"; local tx="${t%,*}"; local tz="${t#*,}"
  "$PY" tools/diag_mca_surface_perch.py "$DEST" "$tx" "$tz" 2>/dev/null | grep -a "==="
  "$PY" tools/diag_floating_trees.py "$DEST" "$tx" "$tz" 2>/dev/null | grep -a "==="
}
export -f one; export DEST PY
printf '%s\n' $TILES | xargs -P 5 -I{} bash -c 'one "$@"' _ {}
echo "=== SWEEP DONE ==="
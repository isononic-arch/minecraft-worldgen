#!/usr/bin/env bash
# _mainland_finalize.sh — extract the 8 mainland collect tarballs to a flat
# region dir on D:, verify counts vs the skip-list expectation, then assemble
# the COMBINED world (mainland + islands + capped ocean generator) on D:.
# Run AFTER render_monitor collects to D:/render_50k_final/_collect/.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
COL="/d/render_50k_final/_collect"
REG="/d/render_50k_final/regions"
SAVES="C:/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves"
SRC_WORLD="${SRC_WORLD:-$SAVES/VandirIslandsV15}"     # datapack shell source
DST_WORLD="${DST_WORLD:-D:/VandirWorld_S105}"          # the combined world

echo "=== 1. extract mainland tarballs -> $REG ==="
mkdir -p "$REG"
shopt -s nullglob
got=0
for t in "$COL"/vandir-50k-*.tgz; do
  echo "  extract $(basename "$t")"
  tar xzf "$t" -C "$REG" --strip-components=1 && got=$((got+1))
done
[ "$got" = 0 ] && { echo "!! no tarballs in $COL"; exit 1; }
n=$(ls "$REG"/r.*.mca 2>/dev/null | wc -l)
echo "  $n mainland regions (expect 9203 = 9409 - 206 island-owned)"

echo "=== 2. assemble combined world ==="
"$PY" islands/assemble_combined_world.py \
  --mainland "$REG" \
  --src "$SRC_WORLD" \
  --dst "$DST_WORLD" \
  2>&1 | grep -viE "NotGeoreferenced|DatasetReader" | tail -25
echo "=== DONE: $DST_WORLD ==="

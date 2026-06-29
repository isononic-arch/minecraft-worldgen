#!/usr/bin/env bash
# _v8_finalize.sh — post-collect for the full 15-island V8: extract all 8 box
# tarballs, assemble a fresh walkable world, topdown the pre-rotated islands to
# verify the Grenada treatment killed the corner slabs. Run AFTER
# _cloud_render_v8.sh collects to islands/_collect_v8/.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
DST="D:/modrinth_vandir/saves/VandirIslandsV8"
SRC="D:/modrinth_vandir/saves/Vandir50k_verify"

echo "=== 1. extract collected tarballs (RELATIVE paths; tar reads C:/ as host) ==="
shopt -s nullglob
got=0
for t in islands/_collect_v8/*.tgz; do echo "  extract $(basename "$t")"; tar xzf "$t" -C . && got=$((got+1)); done
[ "$got" = 0 ] && { echo "!! no tarballs in islands/_collect_v8"; exit 1; }
echo "  extracted $got tarball(s)"
echo "  out/ islands present: $(ls -d islands/out/*/ 2>/dev/null | wc -l)"
for d in islands/out/*/; do echo "    $(basename "$d"): $(ls "$d"r.*.mca 2>/dev/null | wc -l) regions"; done

echo "=== 2. assemble fresh walkable world (all 15 islands) ==="
if [ -d "$DST" ]; then echo "  $DST exists; removing for fresh build"; rm -rf "$DST"; fi
"$PY" islands/make_island_world.py --src "$SRC" --dst "$DST" \
  2>&1 | grep -viE "NotGeoreferenced|DatasetReader"

echo "=== 3. topdown the PRE-ROTATED islands (verify Grenada treatment = no slab) ==="
# token -> safe-name for topdown_fast --name (use DEM tokens; unambiguous)
for tok in 12_445 17_288 18_299 11_863 -1_509 -20_529 -21_008; do
  "$PY" islands/topdown_fast.py --name "$tok" --out "islands/_val/v8_${tok}.png" \
    2>&1 | grep -i saved | grep -v NotGeoreferenced || echo "  topdown $tok FAILED"
done
echo "=== DONE. World: $DST ; prerot topdowns: islands/_val/v8_*.png ==="

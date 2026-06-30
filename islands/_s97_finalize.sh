#!/usr/bin/env bash
# _s97_finalize.sh — post-collect: extract the 3-archipelago render, make topdowns,
# assemble a walkable world. Run AFTER _cloud_render_s97_arch.sh collects to
# islands/_collect_s97/.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
COL="$ROOT/islands/_collect_s97"
DST="D:/modrinth_vandir/saves/VandirIslandsV8_arch"
SRC="D:/modrinth_vandir/saves/Vandir50k_verify"
NAMES_SAFE="grenada_outliers_grenadines kostati_st_vincent_grenadines new_vincentia_st_kitts_nevis_statia"
# DEM tokens (unambiguous for topdown_fast --name; 'grenad' alone hits Kostati)
declare -A TOK=( [grenada_outliers_grenadines]=12_445 [kostati_st_vincent_grenadines]=13_130 [new_vincentia_st_kitts_nevis_statia]=17_288 )

echo "=== 1. extract collected tarballs (RELATIVE paths; tar reads C:/ as host:path) ==="
shopt -s nullglob
# Extract FIRST (relative archive + -C . so tar never sees a C: drive), THEN the
# fresh .mca overwrite the stale ones. (The old order — rm then extract — wiped
# out/ when the C:-path tar failed.)
got=0
for t in islands/_collect_s97/*.tgz; do echo "  extract $(basename "$t")"; tar xzf "$t" -C . && got=$((got+1)); done
[ "$got" = 0 ] && { echo "!! no tarballs in islands/_collect_s97 — render not collected yet?"; exit 1; }
echo "  extracted $got tarball(s)"
for n in $NAMES_SAFE; do
  c=$(ls "islands/out/$n"/r.*.mca 2>/dev/null | wc -l); echo "  out/$n : $c region files"
done

echo "=== 2. topdowns ==="
for n in $NAMES_SAFE; do
  "$PY" islands/topdown_fast.py --name "${TOK[$n]}" --out "islands/_val/td_s97_$n.png" \
    2>&1 | grep -v NotGeoreferenced | grep -i saved || echo "  topdown $n FAILED"
done

echo "=== 3. assemble walkable world ==="
NAMES_CSV=$(echo $NAMES_SAFE | tr ' ' ',')
if [ -d "$DST" ]; then echo "  $DST exists; removing for fresh build"; rm -rf "$DST"; fi
"$PY" islands/make_island_world.py --src "$SRC" --dst "$DST" --names "$NAMES_CSV" \
  2>&1 | grep -viE "NotGeoreferenced|DatasetReader"
echo "=== DONE. World: $DST ; topdowns: islands/_val/td_s97_*.png ==="

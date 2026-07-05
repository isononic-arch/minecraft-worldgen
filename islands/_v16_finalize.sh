#!/usr/bin/env bash
# _v16_finalize.sh — post-collect for the S105 V16 beach-fix render. Run AFTER
# cloud_bake/render_monitor.py has collected islands/_collect_v16/v16-b*.tgz.
#
# Differs from _v8_finalize: SWEEPS each island's islands/out/<isl>/*.mca BEFORE
# extraction (footprints move when bakes change — S104 Efate; stale regions from
# the previous render must not survive into ownership/assembly), and the V16
# tarballs also carry islands/masks_islands (local masks become authoritative
# again — S105 finding). Then: ownership manifest + skip-list REGEN, fresh
# VandirIslandsV16 walk world, verification decodes.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
COL="islands/_collect_v16"
SAVES="C:/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves"
SRC="$SAVES/VandirIslandsV15"
DST="$SAVES/VandirIslandsV16"

echo "=== 1. sweep + extract collected tarballs ==="
shopt -s nullglob
got=0
for t in "$COL"/v16-b*.tgz; do
  echo "  $(basename "$t"):"
  # sweep exactly the islands this tarball carries, then extract (relative paths)
  for isl in $(tar tzf "$t" | grep -o '^islands/out/[^/]*/' | sort -u); do
    n=$(ls "$isl"r.*.mca 2>/dev/null | wc -l)
    echo "    sweep $isl ($n stale regions)"; rm -f "$isl"r.*.mca
  done
  tar xzf "$t" -C . && got=$((got+1))
done
[ "$got" = 0 ] && { echo "!! no tarballs in $COL"; exit 1; }
echo "  extracted $got tarball(s)"
for d in islands/out/*/; do echo "    $(basename "$d"): $(ls "$d"r.*.mca 2>/dev/null | wc -l) regions"; done

echo "=== 2. regen ownership manifest + mainland skip-list (footprints may move) ==="
"$PY" islands/_gen_region_ownership_s101.py 2>&1 | grep -viE "NotGeoreferenced|DatasetReader" | tail -8

echo "=== 3. assemble fresh walk world ==="
if [ -d "$DST" ]; then echo "  $DST exists; removing for fresh build"; rm -rf "$DST"; fi
"$PY" islands/make_island_world.py --src "$SRC" --dst "$DST" \
  2>&1 | grep -viE "NotGeoreferenced|DatasetReader" | tail -12

echo "=== 4. verify ==="
"$PY" - <<'PYEOF'
import json, os
from pathlib import Path
ROOT = Path("C:/Users/nicho/minecraft-worldgen")
own = json.loads((ROOT/"islands"/"region_ownership_s101.json").read_text())["islands"]
bad = 0
for isl, regs in own.items():
    d = ROOT/"islands"/"out"/isl
    disk = {f.name for f in d.glob("r.*.mca")} if d.exists() else set()
    man = {f"r.{x}.{z}.mca" for x, z in regs}
    if disk != man:
        bad += 1
        print(f"  !! {isl}: manifest {len(man)} vs disk {len(disk)} "
              f"(only-manifest {sorted(man-disk)[:3]} only-disk {sorted(disk-man)[:3]})")
print(f"  ownership vs disk: {len(own)-bad}/{len(own)} islands exact")
PYEOF
echo "=== DONE. World: $DST ==="

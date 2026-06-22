#!/usr/bin/env bash
# S94c test-render: fan ~41 high-risk tiles across 5 parallel ccx63 boxes (each
# ~8 tiles = one render batch -> ~12 min wall-clock). Each box: provision -> arm
# 30-min auto-killer -> git reset --hard origin -> bake ocean-dist -> render its
# group -> scp back -> self-delete. Collected to /d/s94c_multi/box{0..4}.
set -u
OUT_ROOT="/d/s94c_multi"
rm -rf "$OUT_ROOT"; mkdir -p "$OUT_ROOT"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

# --- tile groups (cover: water seams, rivers, lakes, deltas, drained,
#     rocky litho+relief mountain block, snow transition, steep grades, ecotone)
G[0]="51,52;52,52;53,52;51,53;52,53;53,53;51,54;52,54;53,54"          # water seam 3x3 (river+lake)
G[1]="69,61;69,62;69,63;89,57;89,58;12,80;13,80;16,76"                # high-alt river chains + wash seam + lake
G[2]="60,72;20,74;20,63;12,82;65,81;35,21;67,54;79,71"                # lakes + deltas + drained/perch
G[3]="73,65;74,65;75,65;73,66;74,66;75,66;73,67;74,67"               # rocky-high mountain block (litho/relief/snow)
G[4]="75,67;73,68;74,68;72,69;33,25;42,29;34,24;55,45"               # mountain rest + steep grades + snow-transition/ecotone

PIDS=()
for i in 0 1 2 3 4; do
  TILES="${G[$i]}" OUT_DIR="$OUT_ROOT/box$i" BOX_NAME="vandir-rwl-$i" TTL_MIN=30 THREADS=8 OMP=4 \
    bash "$HERE/cloud_bake/river_wl_bake.sh" > "$OUT_ROOT/box$i.driver.log" 2>&1 &
  PIDS+=("$!")
  echo "[multibox] launched box$i pid $! tiles=${G[$i]}"
  sleep 6   # stagger Hetzner API calls
done
echo "[multibox] waiting for ${#PIDS[@]} boxes (TTL 30m each)..."
FAIL=0
for p in "${PIDS[@]}"; do wait "$p" || FAIL=$((FAIL+1)); done
echo "=== [multibox] ALL DONE (driver failures=$FAIL) ==="
N=$(ls "$OUT_ROOT"/box*/r.*.mca 2>/dev/null | wc -l)
echo "[multibox] total MCAs collected: $N"
ls "$OUT_ROOT"/box*/r.*.mca 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' '; echo
echo "[multibox] cloud servers remaining (must be 0): $(curl -s -H "Authorization: Bearer $(cat /c/Users/nicho/.hetzner_token)" https://api.hetzner.cloud/v1/servers | C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe -c 'import json,sys;print(len(json.load(sys.stdin).get("servers",[])))')"

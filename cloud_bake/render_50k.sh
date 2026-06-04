#!/usr/bin/env bash
# render_50k.sh — FULL 50k world regen (9,409 tiles = 97x97) across N boxes.
#
# Per-box PARALLELISM: run_pipeline is a ProcessPoolExecutor. We pass --threads
# (default 24) so each CCX63 (48 vCPU, 192 GB) renders ~24 tiles concurrently.
# OMP_NUM_THREADS=2 keeps numpy/scipy from oversubscribing (24*2 = 48 cores).
#
# LOAD BALANCE: z-rows are round-robin-assigned to boxes (box b gets rows
# b, b+NB, b+2NB, ...), so every box gets a mix of land + ocean rows instead of
# one box drawing an all-continent band. Each box renders its rows one at a time
# (full x = 97 tiles per row) via run_pipeline ranges -> naturally RESUMABLE:
# re-run and it overwrites; a dead box only loses its in-progress row.
#
# Usage: bash cloud_bake/render_50k.sh IP1 IP2 ... IP8
# Env:
#   THREADS=24   per-box ProcessPoolExecutor workers
#   DEST=/path   if set, MCAs are copied there after collect (the destination
#                world's region/ folder). Unset -> staged in output_50k/ only.
#
# Wall estimate (8 boxes, --threads 24): ~3-5 h. The monitor prints per-box MCA
# counts every 60s -- after ~20 min the real rate is visible; bump THREADS and
# re-fire if a box is CPU-starved.

set -u
[ "$#" -ge 1 ] || { echo "Usage: $0 IP1 [IP2 ...]"; exit 1; }
IPS=("$@"); NB=${#IPS[@]}
GRID=97
THREADS="${THREADS:-24}"
BRANCH="s85-cherry-picks"
OUT_DIR="output_50k"
DEST="${DEST:-}"
START=$(date +%s)
mkdir -p "$OUT_DIR"

UPLOAD_MASKS=( "masks/override.tif" "masks/lithology.tif" "masks/lithology_region.png" )

log() { echo "[T+$(( ($(date +%s) - START) / 60 ))m] $*"; }

# Round-robin z-rows -> per-box space-separated row list.
declare -a BOX_ROWS
for b in "${!IPS[@]}"; do BOX_ROWS[$b]=""; done
for (( z=0; z<GRID; z++ )); do
  b=$(( z % NB )); BOX_ROWS[$b]="${BOX_ROWS[$b]} $z"
done

prep() {
  local b="$1" ip="${IPS[$b]}" rows="${BOX_ROWS[$b]}"
  local lf="render_50k_${b}.log"; > "$lf"
  echo "[$ip] rows:${rows}" | tee -a "$lf"
  ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null

  ssh root@"$ip" "cd /root/minecraft-worldgen && git fetch origin && \
    (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git reset --hard origin/$BRANCH && git log --oneline -1" 2>&1 | tee -a "$lf"

  ssh root@"$ip" "cd /root/minecraft-worldgen && python3 -" <<'PYEOF' 2>&1 | tee -a "$lf"
import json
p="config/thresholds.json"; d=json.load(open(p))
d["lithology"]["rock_layers"]["enabled"]=True; d["snow_physics"]["enabled"]=True
json.dump(d, open(p,"w"), indent=2); print("flags ON")
PYEOF

  for m in "${UPLOAD_MASKS[@]}"; do
    [ -f "$m" ] || { echo "[$ip] MISSING $m"; return 1; }
    scp -q "$m" root@"$ip":/root/minecraft-worldgen/"$m" 2>&1 | tee -a "$lf"
  done

  # Build derived masks once, then render each assigned z-row (full x).
  local rowcmds=""
  for z in $rows; do
    local z1=$(( z + 1 ))
    rowcmds+="python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 0 --tile-x1 $GRID --tile-z0 $z --tile-z1 $z1 --threads $THREADS >> /root/r50_${z}.log 2>&1; "
  done
  local cmd="cd /root/minecraft-worldgen && rm -f /root/r50_done && rm -rf output /root/r50_*.log && tmux kill-session -t r50 2>/dev/null; "
  cmd+="tmux new -d -s r50 'source /root/venv/bin/activate; export PYTHONUNBUFFERED=1 OMP_NUM_THREADS=2; "
  cmd+="echo BUILD_START > /root/r50_build.log; "
  cmd+="python3 tools/build_terrain_derived.py --only rock_layers,talus,cap --scale 8 >> /root/r50_build.log 2>&1; "
  cmd+="python3 tools/build_snow_physics.py --scale 8 >> /root/r50_build.log 2>&1; "
  cmd+="echo BUILD_DONE >> /root/r50_build.log; "
  cmd+="$rowcmds"
  cmd+="touch /root/r50_done'"
  ssh root@"$ip" "$cmd" 2>&1 | tee -a "$lf"
  echo "[$ip] dispatched ($(echo $rows | wc -w) rows)" | tee -a "$lf"
}

log "Dispatch 9409 tiles across $NB box(es), --threads $THREADS"
for b in "${!IPS[@]}"; do prep "$b" & done
wait
log "All dispatched"

# Monitor: poll MCA counts every 60s until all boxes touch /root/r50_done.
declare -A DONE; for b in "${!IPS[@]}"; do DONE[$b]=0; done
while true; do
  all=1; st=""
  for b in "${!IPS[@]}"; do
    ip="${IPS[$b]}"
    if [ "${DONE[$b]}" = "1" ]; then st="$st b$b=DONE"; continue; fi
    d=$(ssh -o ConnectTimeout=8 root@"$ip" "test -f /root/r50_done && echo Y || echo N" 2>/dev/null)
    n=$(ssh -o ConnectTimeout=8 root@"$ip" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
    if [ "$d" = "Y" ]; then DONE[$b]=1; st="$st b$b=DONE(${n:-?})"; else all=0; st="$st b$b=${n:-0}"; fi
  done
  log "$st"
  [ "$all" = "1" ] && break
  sleep 60
done

log "Collect to $OUT_DIR/"
for b in "${!IPS[@]}"; do
  scp -q root@"${IPS[$b]}":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>/dev/null || true
done
log "$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l) MCAs collected"

if [ -n "$DEST" ] && [ -d "$DEST" ]; then
  cp -f "$OUT_DIR"/*.mca "$DEST/" && log "installed to $DEST"
else
  log "staged in $OUT_DIR/ (set DEST=<world/region> to auto-install)"
fi
log "DONE in $(( ($(date +%s) - START) / 60 ))m"

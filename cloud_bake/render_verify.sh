#!/usr/bin/env bash
# render_verify.sh — render a CURATED tile LIST across N boxes (verification, not full 50k).
# Reads a "tx tz" per-line list, round-robins tiles to boxes, builds derived masks on-box,
# renders each assigned tile, collects via PARALLEL tar-over-ssh (the fast/reliable path).
#
# Usage:
#   TILES=memory/verify_tiles.txt OUT_DIR=/d/verify_out \
#   DEST=/d/modrinth_vandir/saves/Vandir50k/region THREADS=40 OMP=1 \
#   bash cloud_bake/render_verify.sh IP1 IP2 ... IP8
# DEST (optional): auto-copy collected MCAs into that world's region/ for in-world browsing.
set -u
[ "$#" -ge 1 ] || { echo "Usage: $0 IP1 [IP2 ...]"; exit 1; }
IPS=("$@"); NB=${#IPS[@]}
THREADS="${THREADS:-40}"; OMP="${OMP:-1}"
BRANCH="s85-cherry-picks"
TILES="${TILES:-memory/verify_tiles.txt}"
OUT_DIR="${OUT_DIR:-/d/verify_out}"
DEST="${DEST:-}"
START=$(date +%s)
mkdir -p "$OUT_DIR"
UPLOAD_MASKS=( "masks/override.tif" "masks/lithology.tif" "masks/lithology_region.png" )
log(){ echo "[T+$(( ($(date +%s)-START)/60 ))m] $*"; }

mapfile -t TLINES < "$TILES"
declare -a BOX_TILES; for b in "${!IPS[@]}"; do BOX_TILES[$b]=""; done
i=0
for line in "${TLINES[@]}"; do
  [ -z "${line// }" ] && continue
  b=$(( i % NB )); BOX_TILES[$b]="${BOX_TILES[$b]}|$line"; i=$((i+1))
done
log "Rendering $i curated tiles across $NB box(es) from $TILES"

prep(){
  local b="$1" ip="${IPS[$b]}" tiles="${BOX_TILES[$b]}"
  local lf="render_verify_${b}.log"; > "$lf"
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
  local rowcmds=""
  IFS='|' read -ra TS <<< "$tiles"
  for t in "${TS[@]}"; do
    [ -z "${t// }" ] && continue
    local tx=${t%% *} tz=${t##* }; local tx1=$((tx+1)) tz1=$((tz+1))
    rowcmds+="python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 $tx --tile-x1 $tx1 --tile-z0 $tz --tile-z1 $tz1 --threads $THREADS >> /root/rv_${tx}_${tz}.log 2>&1; "
  done
  local cmd="cd /root/minecraft-worldgen && rm -f /root/rv_done && rm -rf output /root/rv_*.log && tmux kill-session -t rv 2>/dev/null; "
  cmd+="tmux new -d -s rv 'source /root/venv/bin/activate; export PYTHONUNBUFFERED=1 OMP_NUM_THREADS=$OMP OPENBLAS_NUM_THREADS=$OMP MKL_NUM_THREADS=$OMP; "
  cmd+="echo BUILD_START > /root/rv_build.log; python3 tools/build_terrain_derived.py --only rock_layers,talus,cap --scale 8 >> /root/rv_build.log 2>&1; "
  cmd+="python3 tools/build_snow_physics.py --scale 8 >> /root/rv_build.log 2>&1; echo BUILD_DONE >> /root/rv_build.log; "
  cmd+="$rowcmds touch /root/rv_done'"
  ssh root@"$ip" "$cmd" 2>&1 | tee -a "$lf"
  echo "[$ip] dispatched ($(echo "$tiles" | tr '|' '\n' | grep -c .) tiles)" | tee -a "$lf"
}
log "Dispatch"
for b in "${!IPS[@]}"; do prep "$b" & done
wait
log "All dispatched. Verify each box pulled $BRANCH tip (grep logs)."

declare -A DONE; for b in "${!IPS[@]}"; do DONE[$b]=0; done
while true; do
  all=1; st=""
  for b in "${!IPS[@]}"; do
    ip="${IPS[$b]}"
    if [ "${DONE[$b]}" = "1" ]; then st="$st b$b=DONE"; continue; fi
    d=$(ssh -o ConnectTimeout=8 root@"$ip" "test -f /root/rv_done && echo Y || echo N" 2>/dev/null)
    n=$(ssh -o ConnectTimeout=8 root@"$ip" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
    if [ "$d" = "Y" ]; then DONE[$b]=1; st="$st b$b=DONE(${n:-?})"; else all=0; st="$st b$b=${n:-0}"; fi
  done
  log "$st"; [ "$all" = "1" ] && break; sleep 30
done

log "Collect (parallel tar-over-ssh) to $OUT_DIR/"
for b in "${!IPS[@]}"; do
  ssh -o ConnectTimeout=20 root@"${IPS[$b]}" "tar cf - -C /root/minecraft-worldgen/output ." 2>/dev/null | tar xf - -C "$OUT_DIR" 2>/dev/null &
done
wait
log "$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l) MCAs collected"
if [ -n "$DEST" ] && [ -d "$DEST" ]; then
  cp -f "$OUT_DIR"/*.mca "$DEST/" && log "installed to $DEST (browse via verify_checklist.md TPs)"
fi
log "DONE in $(( ($(date +%s)-START)/60 ))m"

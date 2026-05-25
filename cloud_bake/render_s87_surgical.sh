#!/usr/bin/env bash
# render_s87_surgical.sh - Surgical 6-tile render for S87 fix verification.
#
# Usage:
#   bash cloud_bake/render_s87_surgical.sh IP1 IP2 IP3 IP4
#
# Tiles chosen to verify recent S87 fixes:
#   (26,10)   BT density + tree-rotation + slope-cutoff
#   (27,13)   SBT after weighting
#   (40,28)   limestone, was rendering as ocean on Vandirtest11
#   (38,15)   limestone fade-band rock_gap
#   (28,7)    pine barrens + rock_gap fade
#   (60,41)   BIRCH after weighting
#
# Distribution: 2 boxes (1+2 tiles each) -- 2 boxes go 2 tiles, 2 boxes 1.
# All 4 boxes used for parallel cache regen.

set -u

if [ "$#" -ne 4 ]; then
  echo "Usage: $0 IP1 IP2 IP3 IP4"
  exit 1
fi

IPS=("$1" "$2" "$3" "$4")
BRANCH="s85-cherry-picks"
LOCAL_OVERRIDE="masks/override.tif"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
OUT_DIR="output_s87_surgical"
START_TIME=$(date +%s)

TILES_BOX1="26,10 27,13"
TILES_BOX2="40,28 38,15"
TILES_BOX3="28,7"
TILES_BOX4="60,41"
TILES_PER_BOX=("$TILES_BOX1" "$TILES_BOX2" "$TILES_BOX3" "$TILES_BOX4")
TOTAL_TILES=6

echo "S87 surgical render: 6 tiles across 4 boxes"
for i in 0 1 2 3; do
  echo "  box $((i+1)) (${IPS[$i]}): ${TILES_PER_BOX[$i]}"
done

step() {
  local elapsed=$(( ($(date +%s) - START_TIME) / 60 ))
  echo ""
  echo "=========================================="
  echo "[T+${elapsed}m] $*"
  echo "=========================================="
}

step "STEP 1/8  SSH host keys"
for IP in "${IPS[@]}"; do
  ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null
  hn=$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$IP" "hostname" 2>&1)
  echo "  $IP -> $hn"
done

step "STEP 2/8  Pull $BRANCH (parallel)"
for IP in "${IPS[@]}"; do
  (ssh root@"$IP" "cd /root/minecraft-worldgen && \
    git fetch && (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git pull && \
    git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png 2>/dev/null; \
    git log --oneline -1" > /tmp/pull_${IP}.log 2>&1 && echo "  $IP pulled") &
done
wait

# S87: SKIP_CACHE_CLEAR=1 env var skips cache wipe for code-only renders.
SKIP_CACHE_CLEAR="${SKIP_CACHE_CLEAR:-0}"
if [ "$SKIP_CACHE_CLEAR" = "1" ]; then
  step "STEP 3/8  Cache clear SKIPPED (SKIP_CACHE_CLEAR=1)"
else
  step "STEP 3/8  Clear caches"
  for IP in "${IPS[@]}"; do
    (ssh root@"$IP" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl /root/minecraft-worldgen/masks/_bed_v17_cache.pkl 2>/dev/null" && echo "  $IP cleared") &
  done
  wait
fi

step "STEP 4/8  Upload override + lithology"
for IP in "${IPS[@]}"; do
  (scp -q "$LOCAL_OVERRIDE" root@"$IP":/root/minecraft-worldgen/masks/override.tif && \
   scp -q masks/lithology.tif root@"$IP":/root/minecraft-worldgen/masks/lithology.tif && \
   scp -q masks/lithology_region.png root@"$IP":/root/minecraft-worldgen/masks/lithology_region.png && \
   echo "  $IP uploaded") &
done
wait

step "STEP 5/8  Dispatch"
dispatch() {
  local IP=$1
  local TILES=$2
  ssh root@"$IP" "cd /root/minecraft-worldgen && rm -rf output /root/render_done /root/render_*.log && tmux kill-session -t render 2>/dev/null; tmux new -d -s render 'source /root/venv/bin/activate; for T in $TILES; do X=\${T%,*}; Z=\${T#*,}; PYTHONUNBUFFERED=1 python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 \$X --tile-x1 \$((X+1)) --tile-z0 \$Z --tile-z1 \$((Z+1)) > /root/render_\${X}_\${Z}.log 2>&1 & done; wait; touch /root/render_done'"
  echo "  $IP dispatched ($TILES)"
}
for i in 0 1 2 3; do
  dispatch "${IPS[$i]}" "${TILES_PER_BOX[$i]}"
done

step "STEP 6/8  Monitor"
LAST_TOTAL=0
STALL=0
while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  TOTAL=0
  DONE=0
  echo "--- T+${ELAPSED}m ---"
  for IP in "${IPS[@]}"; do
    mca=$(ssh root@"$IP" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
    marker=$(ssh root@"$IP" "test -f /root/render_done && echo DONE || echo running" 2>/dev/null)
    echo "  $IP: $mca MCAs ($marker)"
    TOTAL=$((TOTAL + mca))
    [ "$marker" = "DONE" ] && DONE=$((DONE + 1))
  done
  echo "  total: $TOTAL / $TOTAL_TILES, $DONE / 4 boxes done"
  [ "$DONE" -eq 4 ] && { echo "ALL DONE."; break; }
  if [ "$TOTAL" -eq "$LAST_TOTAL" ]; then
    STALL=$((STALL + 1))
    [ "$STALL" -ge 10 ] && echo "  WARN: 10 min no progress"
  else
    STALL=0
  fi
  LAST_TOTAL=$TOTAL
  sleep 60
done

step "STEP 7/8  Collect"
mkdir -p "$OUT_DIR"
for IP in "${IPS[@]}"; do
  scp -q root@"$IP":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1
done
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
echo "  $COLLECTED MCAs in $OUT_DIR/"

step "STEP 8/8  NO auto-install (you copy manually)"
echo "  When ready:  cp $OUT_DIR/*.mca '$VANDIRTEST10/'"

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
echo ""
echo "[DONE] S87 surgical in ${ELAPSED}m. $COLLECTED MCAs."

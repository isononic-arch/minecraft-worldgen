#!/usr/bin/env bash
# render_s86_validation.sh - S86 full validation render on 4 Hetzner CCX63 boxes.
#
# Usage:
#   bash cloud_bake/render_s86_validation.sh IP1 IP2 IP3 IP4
#
# Renders the S86 validation tile set (26 biome refs + 7 user-walk-flagged
# tiles = 33 tiles total) distributed across 4 boxes.  Includes BT-banded
# override + all Phase 1/3 fixes from commit 59b4ec9.
#
# Prerequisites:
#   - 4 CCX63 spun from vandir-s85-2-go snapshot (or anything with Vegetation/)
#   - masks/override.tif locally is the BT-banded version (swap already done)
#
# What's different from render_s86_3x3_4box.sh:
#   - 33-tile set (not 9)
#   - Output to output_s86_validation/
#   - Tile assignments distribute by approximate render-time (large land tiles
#     spread across boxes, simple ocean tiles concentrated)
#
# Wall: ~30-40 min. Cost: ~$1-2.

set -u

if [ "$#" -ne 4 ]; then
  echo "Usage: $0 IP1 IP2 IP3 IP4"
  exit 1
fi

IPS=("$1" "$2" "$3" "$4")
BRANCH="s85-cherry-picks"
LOCAL_OVERRIDE="masks/override.tif"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
OUT_DIR="output_s86_validation"
START_TIME=$(date +%s)

# Tile distribution: ~8 per box. Picked from biome_reference_tiles.csv +
# user-walk feedback tiles (15,61 / 89,52 / 10,77 lithology tests,
# 28,7 / 26,10 / 32,10 / 17,41 / 36,75 / 20,36 / 50,48 user walks).
TILES_BOX1="37,8 27,9 32,13 32,31 13,82 39,23 30,49 6,68 26,10"
TILES_BOX2="23,29 27,13 33,6 80,50 29,76 60,41 50,50 32,10 17,41"
TILES_BOX3="59,44 18,66 19,63 27,65 30,90 31,89 8,73 20,36 15,61"
TILES_BOX4="34,9 28,35 85,79 38,15 40,28 89,52 10,77 28,7 36,75"
TILES_PER_BOX=("$TILES_BOX1" "$TILES_BOX2" "$TILES_BOX3" "$TILES_BOX4")
TOTAL_TILES=$(echo "$TILES_BOX1 $TILES_BOX2 $TILES_BOX3 $TILES_BOX4" | wc -w)

echo "S86 validation render plan:"
echo "  total tiles: $TOTAL_TILES"
for i in 0 1 2 3; do
  echo "  box $((i+1)) (${IPS[$i]}): ${TILES_PER_BOX[$i]}"
done
echo "  override: $LOCAL_OVERRIDE (must be BT-banded version)"
echo ""

step() {
  local elapsed=$(( ($(date +%s) - START_TIME) / 60 ))
  echo ""
  echo "=========================================="
  echo "[T+${elapsed}m] $*"
  echo "=========================================="
}

# STEP 1: SSH host keys
step "STEP 1/9  SSH host keys + sanity"
for IP in "${IPS[@]}"; do
  ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null
  hn=$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$IP" "hostname" 2>&1)
  echo "  $IP -> $hn"
  if [[ "$hn" == *"denied"* || "$hn" == *"timed out"* ]]; then
    echo "  ERROR: $IP not reachable"
    exit 1
  fi
done

# STEP 2: Pull branch on all 4 in parallel
step "STEP 2/9  Pulling '$BRANCH' (parallel)"
for IP in "${IPS[@]}"; do
  (ssh root@"$IP" "cd /root/minecraft-worldgen && \
    rm -f masks/hydro_region.png.bak masks/lithology_region.png.bak masks/override.tif.bak_s84_pre_lrfc_north_strip 2>/dev/null; \
    git fetch && \
    (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git pull && \
    git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png 2>/dev/null; \
    git log --oneline -1" > /tmp/render_pull_${IP}.log 2>&1 && echo "  $IP pull done") &
done
wait

# STEP 3: Clear caches (parallel)
step "STEP 3/9  Clearing stale mask caches"
for IP in "${IPS[@]}"; do
  (ssh root@"$IP" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl /root/minecraft-worldgen/masks/_bed_v17_cache.pkl 2>/dev/null" && echo "  $IP caches cleared") &
done
wait

# STEP 4: Upload override.tif + lithology files (parallel, scp not rsync)
step "STEP 4/9  Uploading override.tif + lithology files"
for IP in "${IPS[@]}"; do
  (ssh root@"$IP" "cp /root/minecraft-worldgen/masks/override.tif /root/minecraft-worldgen/masks/override.tif.pre_s86 2>/dev/null" && \
   scp -q "$LOCAL_OVERRIDE" root@"$IP":/root/minecraft-worldgen/masks/override.tif && \
   scp -q masks/lithology.tif root@"$IP":/root/minecraft-worldgen/masks/lithology.tif && \
   scp -q masks/lithology_region.png root@"$IP":/root/minecraft-worldgen/masks/lithology_region.png && \
   echo "  $IP: override + lithology uploaded") &
done
wait

# STEP 5: Verify Vegetation/
step "STEP 5/9  Verifying Vegetation/"
NEEDS_UPLOAD=""
for IP in "${IPS[@]}"; do
  HAS=$(ssh root@"$IP" "ls /root/minecraft-worldgen/Vegetation 2>/dev/null | wc -l")
  if [ "$HAS" -lt 900 ]; then
    NEEDS_UPLOAD="$NEEDS_UPLOAD $IP"
    echo "  $IP: only $HAS Vegetation files - needs upload"
  else
    echo "  $IP: $HAS Vegetation files OK"
  fi
done
if [ -n "$NEEDS_UPLOAD" ]; then
  for IP in $NEEDS_UPLOAD; do
    (scp -r -q Vegetation/ root@"$IP":/root/minecraft-worldgen/ && echo "  $IP Vegetation done") &
  done
  wait
fi

# STEP 6: Dispatch
step "STEP 6/9  Dispatching tiles"
dispatch() {
  local IP=$1
  local TILES=$2
  ssh root@"$IP" "cd /root/minecraft-worldgen && rm -rf output /root/render_done /root/render_*.log && tmux kill-session -t render 2>/dev/null; tmux new -d -s render 'source /root/venv/bin/activate; for T in $TILES; do X=\${T%,*}; Z=\${T#*,}; PYTHONUNBUFFERED=1 python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 \$X --tile-x1 \$((X+1)) --tile-z0 \$Z --tile-z1 \$((Z+1)) > /root/render_\${X}_\${Z}.log 2>&1 & done; wait; touch /root/render_done'"
  echo "  $IP dispatched ($TILES)"
}
for i in 0 1 2 3; do
  dispatch "${IPS[$i]}" "${TILES_PER_BOX[$i]}"
done

# STEP 7: Monitor
step "STEP 7/9  Monitoring"
LAST_TOTAL=0
STALL_COUNT=0
while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  TOTAL_MCAS=0
  DONE_COUNT=0
  echo "--- T+${ELAPSED}m ---"
  for IP in "${IPS[@]}"; do
    mca=$(ssh root@"$IP" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
    marker=$(ssh root@"$IP" "test -f /root/render_done && echo DONE || echo running" 2>/dev/null)
    echo "  $IP: $mca MCAs ($marker)"
    TOTAL_MCAS=$((TOTAL_MCAS + mca))
    [ "$marker" = "DONE" ] && DONE_COUNT=$((DONE_COUNT + 1))
  done
  echo "  total: $TOTAL_MCAS / $TOTAL_TILES MCAs, $DONE_COUNT / 4 boxes done"
  if [ "$DONE_COUNT" -eq 4 ]; then
    echo "  ALL DONE."
    break
  fi
  if [ "$TOTAL_MCAS" -eq "$LAST_TOTAL" ]; then
    STALL_COUNT=$((STALL_COUNT + 1))
    [ "$STALL_COUNT" -ge 10 ] && echo "  WARN: no MCAs in ~10 min"
  else
    STALL_COUNT=0
  fi
  LAST_TOTAL=$TOTAL_MCAS
  sleep 60
done

# STEP 8: Collect
step "STEP 8/9  Collecting MCAs"
mkdir -p "$OUT_DIR"
for IP in "${IPS[@]}"; do
  scp -q root@"$IP":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1
done
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
echo "  collected: $COLLECTED MCAs in ./$OUT_DIR/"

# STEP 9: NO auto-install (user walks S85 still possibly)
step "STEP 9/9  MCAs left in $OUT_DIR"
echo "  $COLLECTED MCAs ready at: $(pwd)/$OUT_DIR/"
echo ""
echo "When ready to view:"
echo "  cp $OUT_DIR/*.mca '$VANDIRTEST10/'"

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
echo ""
echo "=========================================="
echo "[DONE] S86 validation render complete in ${ELAPSED}m"
echo "  Tiles rendered: $COLLECTED / $TOTAL_TILES"
echo "=========================================="
echo ""
echo "Don't forget to DELETE the 4 boxes in Hetzner Console after collection."

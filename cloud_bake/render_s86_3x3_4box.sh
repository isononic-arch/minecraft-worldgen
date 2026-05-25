#!/usr/bin/env bash
# render_s86_3x3_4box.sh - S86 BT-banding 3x3 validation on 4 Hetzner CCX63 boxes.
#
# Usage:
#   bash cloud_bake/render_s86_3x3_4box.sh CENTER_X CENTER_Z IP1 IP2 IP3 IP4
#
# Same as render_s86_3x3.sh but splits 9 tiles 3-2-2-2 across 4 boxes for
# minimum wall time. Each box runs in parallel.
#
# Prerequisites:
#   - 4 CCX63 spun from vandir-s85-2-go (or any snapshot with Vegetation/ baked in)
#   - masks/override_s86_BT_bands.tif exists locally
#
# Step 9 does NOT auto-copy MCAs into Vandirtest10 (user is walking S85 in-game).

set -u

if [ "$#" -ne 6 ]; then
  echo "Usage: $0 CENTER_X CENTER_Z IP1 IP2 IP3 IP4"
  exit 1
fi

CX="$1"; CZ="$2"
IPS=("$3" "$4" "$5" "$6")
BRANCH="s85-cherry-picks"
LOCAL_OVERRIDE="masks/override_s86_BT_bands.tif"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
OUT_DIR="output_s86_3x3"
START_TIME=$(date +%s)

if [ ! -f "$LOCAL_OVERRIDE" ]; then
  echo "ERROR: $LOCAL_OVERRIDE not found."
  exit 1
fi

# Build 3x3 tile list, then distribute 3-2-2-2 across 4 boxes
TILES_ALL=()
for DX in -1 0 1; do
  for DZ in -1 0 1; do
    X=$((CX + DX)); Z=$((CZ + DZ))
    if [ $X -lt 0 ] || [ $X -gt 96 ] || [ $Z -lt 0 ] || [ $Z -gt 96 ]; then
      echo "  WARN: tile ($X, $Z) out of bounds, skipping"
      continue
    fi
    TILES_ALL+=("$X,$Z")
  done
done

# Distribute: box 1 gets first 3, boxes 2-4 get 2 each
TILES_BOX1="${TILES_ALL[0]} ${TILES_ALL[1]} ${TILES_ALL[2]}"
TILES_BOX2="${TILES_ALL[3]} ${TILES_ALL[4]}"
TILES_BOX3="${TILES_ALL[5]} ${TILES_ALL[6]}"
TILES_BOX4="${TILES_ALL[7]} ${TILES_ALL[8]}"
TILES_PER_BOX=("$TILES_BOX1" "$TILES_BOX2" "$TILES_BOX3" "$TILES_BOX4")

echo "S86 3x3 (4-box) render plan:"
echo "  center: ($CX, $CZ)"
echo "  total tiles: ${#TILES_ALL[@]}"
echo "  box 1 (${IPS[0]}): $TILES_BOX1"
echo "  box 2 (${IPS[1]}): $TILES_BOX2"
echo "  box 3 (${IPS[2]}): $TILES_BOX3"
echo "  box 4 (${IPS[3]}): $TILES_BOX4"
echo "  override: $LOCAL_OVERRIDE -> masks/override.tif on each box"
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
step "STEP 2/9  Pulling '$BRANCH' on all 4 boxes (parallel)"
for IP in "${IPS[@]}"; do
  (ssh root@"$IP" "cd /root/minecraft-worldgen && \
    rm -f masks/hydro_region.png.bak masks/lithology_region.png.bak masks/override.tif.bak_s84_pre_lrfc_north_strip 2>/dev/null; \
    git fetch && \
    (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git pull && \
    git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png masks/hydro_region.png.bak masks/lithology_region.png.bak masks/override.tif.bak_s84_pre_lrfc_north_strip 2>/dev/null; \
    git log --oneline -1" > /tmp/render_pull_${IP}.log 2>&1 && echo "  $IP pull done") &
done
wait

# STEP 3: Clear caches (parallel)
step "STEP 3/9  Clearing stale mask caches (parallel)"
for IP in "${IPS[@]}"; do
  (ssh root@"$IP" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl /root/minecraft-worldgen/masks/_bed_v17_cache.pkl 2>/dev/null" && echo "  $IP caches cleared") &
done
wait

# STEP 4: Upload override.tif + lithology.tif + lithology_region.png to each box (parallel)
# S86: scp is used instead of rsync (Git Bash on Windows doesn't have rsync).
# Lithology files sync because the snapshot can lag the user's recent paints —
# stale lithology.tif on snapshot was the root cause of (15,61) / (89,52) /
# (10,77) wrong-palette reports in the S85 walk.
step "STEP 4/9  Uploading override.tif + lithology files on all 4 boxes (parallel)"
for IP in "${IPS[@]}"; do
  (ssh root@"$IP" "cp /root/minecraft-worldgen/masks/override.tif /root/minecraft-worldgen/masks/override.tif.pre_s86 2>/dev/null" && \
   scp -q "$LOCAL_OVERRIDE" root@"$IP":/root/minecraft-worldgen/masks/override.tif && \
   scp -q masks/lithology.tif root@"$IP":/root/minecraft-worldgen/masks/lithology.tif && \
   scp -q masks/lithology_region.png root@"$IP":/root/minecraft-worldgen/masks/lithology_region.png && \
   echo "  $IP: override + lithology uploaded") &
done
wait

# STEP 5: Verify Vegetation/ on each box (parallel quick check)
step "STEP 5/9  Verifying Vegetation/ on all 4 boxes"
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
  echo "  Uploading Vegetation/ to: $NEEDS_UPLOAD (parallel)"
  for IP in $NEEDS_UPLOAD; do
    (scp -r -q Vegetation/ root@"$IP":/root/minecraft-worldgen/ && echo "  $IP Vegetation upload done") &
  done
  wait
fi

# STEP 6: Dispatch render on all 4 boxes
step "STEP 6/9  Dispatching tiles to all 4 boxes (parallel)"
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
step "STEP 7/9  Monitoring (first tile per box ~10 min cache regen; subsequent ~3-5 min)"
TOTAL_TILES=${#TILES_ALL[@]}
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
    if [ "$STALL_COUNT" -ge 10 ]; then
      echo "  WARN: no MCAs produced in ~10 min. Check render_*.log on the boxes."
    fi
  else
    STALL_COUNT=0
  fi
  LAST_TOTAL=$TOTAL_MCAS
  sleep 60
done

# STEP 8: Collect
step "STEP 8/9  Collecting MCAs to laptop"
mkdir -p "$OUT_DIR"
for IP in "${IPS[@]}"; do
  scp -q root@"$IP":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1
done
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
echo "  collected: $COLLECTED MCAs in ./$OUT_DIR/"

# STEP 9: NO auto-install
step "STEP 9/9  MCAs left in $OUT_DIR (NOT auto-copied to Vandirtest10)"
echo "  $COLLECTED MCAs ready at: $(pwd)/$OUT_DIR/"
echo ""
echo "When done with S85 walk and ready to view S86 3x3:"
echo "  cp $OUT_DIR/*.mca '$VANDIRTEST10/'"

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
echo ""
echo "=========================================="
echo "[DONE] S86 3x3 4-box render complete in ${ELAPSED}m"
echo "  Tiles rendered: $COLLECTED / $TOTAL_TILES"
echo "  Center TP after copy: /tp @s $((CX*512+256)) 200 $((CZ*512+256))"
echo "=========================================="
echo ""
echo "Don't forget to DELETE the 4 boxes in Hetzner Console after collection."

#!/usr/bin/env bash
# render_s87_walk3.sh - S87 walk #3 verification render: 16 tiles, 4 boxes.
#
# Tiles cover every fix from the walk#3 bundle (commit 3113ee2):
#   34,9    KARST short_grass (#9) + mountain faces
#   38,11   sub-slope flat-detect tune (#11) + washes spared
#   27,9    BA control + new density
#   26,10   BT new density + BT tree range tightening + GC swap fix
#   27,13   SBT new density
#   80,50   RIPARIAN no-swap + GC swap fix
#   30,86   MANGROVE
#   59,44   slope cutoff back off + GC swap fix
#   32,13   AT
#   28,7    sand blobs gone + pine barrens density + GC swap fix
#   8,73    FEN
#   60,41   birch new density
#   50,48   mixed (re-render -- previous tile didn't upload)
#   36,75   maquis bush massive bump
#   13,82   RFC palette restructure + density
#   36,15   *NEW* big big flat karst cliff (77% karst, ~200 block range)
#
# Auto-installs to Vandirtest10.

set -u

if [ "$#" -ne 4 ]; then
  echo "Usage: $0 IP1 IP2 IP3 IP4"
  exit 1
fi

IPS=("$1" "$2" "$3" "$4")
BRANCH="s85-cherry-picks"
LOCAL_OVERRIDE="masks/override.tif"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
OUT_DIR="output_s87_walk3"
START_TIME=$(date +%s)

TILES_BOX1="34,9 38,11 27,9 26,10"
TILES_BOX2="27,13 80,50 30,86 59,44"
TILES_BOX3="32,13 28,7 8,73 60,41"
TILES_BOX4="50,48 36,75 13,82 36,15"
TILES_PER_BOX=("$TILES_BOX1" "$TILES_BOX2" "$TILES_BOX3" "$TILES_BOX4")
TOTAL_TILES=16

echo "S87 walk #3 render: 16 tiles, 4 boxes"
for i in 0 1 2 3; do
  echo "  box $((i+1)) (${IPS[$i]}): ${TILES_PER_BOX[$i]}"
done

step() {
  local elapsed=$(( ($(date +%s) - START_TIME) / 60 ))
  echo ""
  echo "[T+${elapsed}m] $*"
}

step "STEP 1/9  SSH host keys"
for IP in "${IPS[@]}"; do
  ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null
  hn=$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$IP" "hostname" 2>&1)
  echo "  $IP -> $hn"
done

step "STEP 2/9  Pull $BRANCH (parallel)"
for IP in "${IPS[@]}"; do
  (ssh root@"$IP" "cd /root/minecraft-worldgen && \
    git fetch && (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git pull && \
    git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png 2>/dev/null; \
    git log --oneline -1" > /tmp/pull_${IP}.log 2>&1 && echo "  $IP pulled") &
done
wait

# S87: skip cache clear for code-only iterations to save ~10 min cache regen.
# Caches (_bed_cache_v17.pkl, _spline_cache.pkl) are masks-derived only -
# safe to keep when only Python or config-non-spline files changed.
# Invoke with `SKIP_CACHE_CLEAR=1 bash ...` to skip.
SKIP_CACHE_CLEAR="${SKIP_CACHE_CLEAR:-0}"
if [ "$SKIP_CACHE_CLEAR" = "1" ]; then
  step "STEP 3/9  Cache clear SKIPPED (SKIP_CACHE_CLEAR=1)"
else
  step "STEP 3/9  Clear caches"
  for IP in "${IPS[@]}"; do
    (ssh root@"$IP" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl /root/minecraft-worldgen/masks/_bed_v17_cache.pkl 2>/dev/null" && echo "  $IP cleared") &
  done
  wait
fi

step "STEP 4/9  Upload override + lithology"
for IP in "${IPS[@]}"; do
  (scp -q "$LOCAL_OVERRIDE" root@"$IP":/root/minecraft-worldgen/masks/override.tif && \
   scp -q masks/lithology.tif root@"$IP":/root/minecraft-worldgen/masks/lithology.tif && \
   scp -q masks/lithology_region.png root@"$IP":/root/minecraft-worldgen/masks/lithology_region.png && \
   echo "  $IP uploaded") &
done
wait

step "STEP 5/9  Dispatch"
dispatch() {
  local IP=$1
  local TILES=$2
  ssh root@"$IP" "cd /root/minecraft-worldgen && rm -rf output /root/render_done /root/render_*.log && tmux kill-session -t render 2>/dev/null; tmux new -d -s render 'source /root/venv/bin/activate; for T in $TILES; do X=\${T%,*}; Z=\${T#*,}; PYTHONUNBUFFERED=1 python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 \$X --tile-x1 \$((X+1)) --tile-z0 \$Z --tile-z1 \$((Z+1)) > /root/render_\${X}_\${Z}.log 2>&1 & done; wait; touch /root/render_done'"
  echo "  $IP dispatched ($TILES)"
}
for i in 0 1 2 3; do
  dispatch "${IPS[$i]}" "${TILES_PER_BOX[$i]}"
done

step "STEP 6/9  Monitor"
LAST=0; STALL=0
while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  TOTAL=0; DONE=0
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
  if [ "$TOTAL" -eq "$LAST" ]; then
    STALL=$((STALL + 1))
    [ "$STALL" -ge 10 ] && echo "  WARN: 10 min no progress"
  else
    STALL=0
  fi
  LAST=$TOTAL
  sleep 60
done

step "STEP 7/9  Collect"
mkdir -p "$OUT_DIR"
for IP in "${IPS[@]}"; do
  scp -q root@"$IP":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1
done
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
echo "  $COLLECTED MCAs in $OUT_DIR/"

step "STEP 8/9  AUTO-INSTALL to Vandirtest10"
if [ -d "$VANDIRTEST10" ]; then
  cp -f "$OUT_DIR"/*.mca "$VANDIRTEST10/" 2>&1
  FINAL=$(ls "$VANDIRTEST10/" | grep -c "\.mca$")
  echo "  Vandirtest10/region/ now has $FINAL MCAs"
else
  echo "  WARN: $VANDIRTEST10 not found"
fi

step "STEP 9/9  DONE"
ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
echo "  S87 walk #3 render done in ${ELAPSED}m, $COLLECTED MCAs"
echo ""
echo "Walk tile TPs:"
echo "  /tp @s 17663 160 4863    # 34,9 KARST"
echo "  /tp @s 19712 140 5888    # 38,11 limestone (washes/flat-detect)"
echo "  /tp @s 14079 250 4863    # 27,9 BA"
echo "  /tp @s 13568 200 5376    # 26,10 BT"
echo "  /tp @s 14079 220 6911    # 27,13 SBT"
echo "  /tp @s 41317 100 25836   # 80,50 RIPARIAN"
echo "  /tp @s 15688 80 44371    # 30,86 MANGROVE"
echo "  /tp @s 30463 200 22783   # 59,44 BT slopes"
echo "  /tp @s 16639 180 6911    # 32,13 AT"
echo "  /tp @s 14592 140 3840    # 28,7 sand blobs / pine barrens"
echo "  /tp @s 4358 80 37642     # 8,73 FEN"
echo "  /tp @s 30975 180 21247   # 60,41 birch"
echo "  /tp @s 25855 200 25855   # 50,48 mixed (re-render)"
echo "  /tp @s 18688 140 38656   # 36,75 maquis (bushes)"
echo "  /tp @s 6911 90 42239     # 13,82 RFC"
echo "  /tp @s 18688 250 7935    # 36,15 BIG KARST CLIFF"

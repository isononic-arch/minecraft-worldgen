#!/usr/bin/env bash
# render_single_tile.sh - Render ONE tile on ONE box for fast iteration.
#
# Usage:
#   bash cloud_bake/render_single_tile.sh TX TZ IP
#
# Env:
#   SKIP_CACHE_CLEAR=1 -- skip the cache wipe (code-only iterations)
#
# Example:
#   SKIP_CACHE_CLEAR=1 bash cloud_bake/render_single_tile.sh 36 15 178.104.217.236
#
# Auto-installs to Vandirtest10.

set -u

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 TX TZ IP"
  exit 1
fi

TX="$1"; TZ="$2"; IP="$3"
BRANCH="s85-cherry-picks"
LOCAL_OVERRIDE="masks/override.tif"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
OUT_DIR="output_single_${TX}_${TZ}"
START_TIME=$(date +%s)
SKIP_CACHE_CLEAR="${SKIP_CACHE_CLEAR:-0}"

step() {
  local elapsed=$(( ($(date +%s) - START_TIME) / 60 ))
  echo ""
  echo "[T+${elapsed}m] $*"
}

step "STEP 1  SSH host key"
ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null
hn=$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$IP" "hostname" 2>&1)
echo "  $IP -> $hn"

step "STEP 2  Pull $BRANCH"
ssh root@"$IP" "cd /root/minecraft-worldgen && \
  git fetch && (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
  git pull && \
  git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png 2>/dev/null; \
  git log --oneline -1"

if [ "$SKIP_CACHE_CLEAR" = "1" ]; then
  step "STEP 3  Cache clear SKIPPED"
else
  step "STEP 3  Clear caches"
  ssh root@"$IP" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl /root/minecraft-worldgen/masks/_bed_v17_cache.pkl 2>/dev/null"
fi

step "STEP 4  Upload override + lithology"
scp -q "$LOCAL_OVERRIDE" root@"$IP":/root/minecraft-worldgen/masks/override.tif
scp -q masks/lithology.tif root@"$IP":/root/minecraft-worldgen/masks/lithology.tif
scp -q masks/lithology_region.png root@"$IP":/root/minecraft-worldgen/masks/lithology_region.png

step "STEP 5  Dispatch ($TX, $TZ)"
TX1=$((TX + 1)); TZ1=$((TZ + 1))
ssh root@"$IP" "cd /root/minecraft-worldgen && rm -rf output /root/render_done /root/render_*.log && tmux kill-session -t render 2>/dev/null; tmux new -d -s render 'source /root/venv/bin/activate; PYTHONUNBUFFERED=1 python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 $TX --tile-x1 $TX1 --tile-z0 $TZ --tile-z1 $TZ1 > /root/render_${TX}_${TZ}.log 2>&1; touch /root/render_done'"
echo "  dispatched"

step "STEP 6  Monitor"
while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  marker=$(ssh root@"$IP" "test -f /root/render_done && echo DONE || echo running" 2>/dev/null)
  mca=$(ssh root@"$IP" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
  echo "  T+${ELAPSED}m: $mca MCAs ($marker)"
  [ "$marker" = "DONE" ] && break
  sleep 30
done

step "STEP 7  Collect"
mkdir -p "$OUT_DIR"
scp -q root@"$IP":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
echo "  $COLLECTED MCAs in $OUT_DIR/"

step "STEP 8  Auto-install"
if [ -d "$VANDIRTEST10" ]; then
  cp -f "$OUT_DIR"/*.mca "$VANDIRTEST10/"
  echo "  installed to Vandirtest10"
fi

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
echo ""
echo "[DONE] in ${ELAPSED}m"
echo "  /tp @s $((TX*512+256)) 200 $((TZ*512+256))   # ($TX,$TZ) center"

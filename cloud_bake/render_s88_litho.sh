#!/usr/bin/env bash
# render_s88_litho.sh — 4-tile pure-litho render for visual reference.
#
# Pure tiles (100% one biome -> 100% one lithology group):
#   (50,50)  MIXED_FOREST           granitic
#   (23,29)  TEMPERATE_RAINFOREST   mossy_temperate
#   (27,9)   BOREAL_ALPINE          deepslate_metamorphic (mountain)
#   (32,13)  ARCTIC_TUNDRA          deepslate_metamorphic (arctic)
#
# Use after a strata code+config push to inspect each litho's bands +
# speckle + vein behavior in isolation.

set -u

if [ "$#" -ne 4 ]; then
  echo "Usage: $0 IP1 IP2 IP3 IP4"
  echo "  IP1 -> (50,50) granitic MIXED_FOREST"
  echo "  IP2 -> (23,29) mossy_temperate TEMPERATE_RAINFOREST"
  echo "  IP3 -> (27,9)  deepslate_metamorphic BOREAL_ALPINE"
  echo "  IP4 -> (32,13) deepslate_metamorphic ARCTIC_TUNDRA"
  exit 1
fi

IPS=("$1" "$2" "$3" "$4")
TXS=(50 23 27 32)
TZS=(50 29  9 13)
NAMES=("granitic-MIXED_FOREST" "mossy-TEMPERATE_RAINFOREST" "deepslate-BOREAL_ALPINE" "deepslate-ARCTIC_TUNDRA")

BRANCH="s85-cherry-picks"
OUT_DIR="output_s88_litho"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
START_TIME=$(date +%s)
SKIP_CACHE_CLEAR="${SKIP_CACHE_CLEAR:-1}"  # default skip; masks unchanged
NO_INSTALL="${NO_INSTALL:-0}"
INSTALL="${INSTALL:-1}"

UPLOAD_MASKS=(
  "masks/override.tif"
  "masks/lithology.tif"
  "masks/lithology_region.png"
  "masks/aspect.tif"
  "masks/cliff_cap.tif"
  "masks/talus_apron.tif"
  "masks/bedrock_drainage.tif"
)

log() {
  local elapsed=$(( ($(date +%s) - START_TIME) / 60 ))
  echo "[T+${elapsed}m] $*"
}

prep_and_dispatch_one() {
  local idx="$1"
  local ip="${IPS[$idx]}"
  local tx="${TXS[$idx]}"
  local tz="${TZS[$idx]}"
  local name="${NAMES[$idx]}"
  local tag="[$ip ($tx,$tz) $name]"
  local log_file="render_s88_litho_${tx}_${tz}.log"
  > "$log_file"

  echo "$tag prep start" | tee -a "$log_file"
  ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null
  ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$ip" "hostname" 2>&1 | tee -a "$log_file"

  ssh root@"$ip" "cd /root/minecraft-worldgen && \
    git fetch && (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git pull && \
    git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png 2>/dev/null; \
    git log --oneline -2" 2>&1 | tee -a "$log_file"

  if [ "$SKIP_CACHE_CLEAR" = "1" ]; then
    echo "$tag cache clear SKIPPED" | tee -a "$log_file"
  else
    echo "$tag clearing caches" | tee -a "$log_file"
    ssh root@"$ip" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl 2>/dev/null"
  fi

  echo "$tag uploading ${#UPLOAD_MASKS[@]} masks" | tee -a "$log_file"
  for m in "${UPLOAD_MASKS[@]}"; do
    [ -f "$m" ] || { echo "$tag MISSING $m" | tee -a "$log_file"; return 1; }
    scp -q "$m" root@"$ip":/root/minecraft-worldgen/"$m" 2>&1 | tee -a "$log_file"
  done

  local tx1=$((tx + 1)); local tz1=$((tz + 1))
  ssh root@"$ip" "cd /root/minecraft-worldgen && rm -rf output /root/render_done /root/render_*.log && tmux kill-session -t render 2>/dev/null; tmux new -d -s render 'source /root/venv/bin/activate; PYTHONUNBUFFERED=1 python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 $tx --tile-x1 $tx1 --tile-z0 $tz --tile-z1 $tz1 > /root/render_${tx}_${tz}.log 2>&1; touch /root/render_done'" 2>&1 | tee -a "$log_file"
  echo "$tag dispatched OK" | tee -a "$log_file"
}

log "STEP 1-4  Parallel prep + dispatch"
for idx in 0 1 2 3; do
  prep_and_dispatch_one "$idx" &
done
wait
log "All boxes dispatched"

log "STEP 5  Monitor"
mkdir -p "$OUT_DIR"
declare -A DONE
for idx in 0 1 2 3; do DONE[$idx]=0; done
while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  all_done=1; status=""
  for idx in 0 1 2 3; do
    if [ "${DONE[$idx]}" = "1" ]; then
      status="${status}  (${TXS[$idx]},${TZS[$idx]})=DONE"
      continue
    fi
    ip="${IPS[$idx]}"
    marker=$(ssh -o ConnectTimeout=5 root@"$ip" "test -f /root/render_done && echo DONE || echo running" 2>/dev/null)
    if [ "$marker" = "DONE" ]; then
      DONE[$idx]=1
      status="${status}  (${TXS[$idx]},${TZS[$idx]})=DONE"
    else
      all_done=0
      mca=$(ssh -o ConnectTimeout=5 root@"$ip" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
      status="${status}  (${TXS[$idx]},${TZS[$idx]})=${mca:-0}mca"
    fi
  done
  echo "  T+${ELAPSED}m:$status"
  [ "$all_done" = "1" ] && break
  sleep 30
done

log "STEP 6  Collecting"
for idx in 0 1 2 3; do
  ip="${IPS[$idx]}"
  scp -q root@"$ip":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1 || true
done
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
log "$COLLECTED MCAs in $OUT_DIR/"

log "STEP 7  Hash verify"
for f in "$OUT_DIR"/*.mca; do
  [ -f "$f" ] && md5sum "$f"
done

if [ "$INSTALL" = "1" ]; then
  log "STEP 8  Installing to Vandirtest10"
  [ -d "$VANDIRTEST10" ] && cp -f "$OUT_DIR"/*.mca "$VANDIRTEST10/" && log "  installed"
else
  log "STEP 8  Install SKIPPED -- MCAs in $OUT_DIR/."
fi

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
log "[DONE] in ${ELAPSED}m"
echo ""
echo "Teleport coords:"
for idx in 0 1 2 3; do
  tx="${TXS[$idx]}"; tz="${TZS[$idx]}"; name="${NAMES[$idx]}"
  echo "  /tp @s $((tx*512+256)) 200 $((tz*512+256))   # ($tx,$tz) $name"
done

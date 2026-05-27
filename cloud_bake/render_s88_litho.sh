#!/usr/bin/env bash
# render_s88_litho.sh — 6-tile pure-litho CLIFF render for strata visual reference.
#
# Verified vs ACTUAL masks/lithology.tif (NOT biome zone_to_group):
#   (72,60)  granitic               Y_tilted   100%  211k rock_gap
#   (24,80)  arid_basaltic          XZ_cols    100%  218k rock_gap   [walk #4c]
#   (89,52)  temperate_basaltic     XZ_cols    100%  244k rock_gap   [walk #4c]
#   (36,15)  limestone              Y_tilted    92%  135k rock_gap   (karst tile)
#   (19,44)  deepslate_metamorphic  Y_tilted   100%  217k rock_gap
#   (63,72)  mossy_temperate        Y_tilted   100%   95k rock_gap
#
# Use after a strata code+config push to inspect each litho's bands +
# speckle + vein behavior in isolation, with VISIBLE CLIFF FACES.

set -u

if [ "$#" -ne 6 ]; then
  echo "Usage: $0 IP1 IP2 IP3 IP4 IP5 IP6"
  echo "  IP1 -> (72,60) granitic                Y_tilted  211k rock_gap"
  echo "  IP2 -> (24,80) arid_basaltic           XZ_cols   218k rock_gap  [walk #4c]"
  echo "  IP3 -> (89,52) temperate_basaltic      XZ_cols   244k rock_gap  [walk #4c]"
  echo "  IP4 -> (36,15) limestone               Y_tilted  135k rock_gap  (karst)"
  echo "  IP5 -> (19,44) deepslate_metamorphic   Y_tilted  217k rock_gap"
  echo "  IP6 -> (63,72) mossy_temperate         Y_tilted   95k rock_gap"
  exit 1
fi

IPS=("$1" "$2" "$3" "$4" "$5" "$6")
TXS=(72 24 89 36 19 63)
TZS=(60 80 52 15 44 72)
NAMES=("granitic-CLIFF" "arid_basaltic-CLIFF" "temperate_basaltic-CLIFF" "limestone-CLIFF" "deepslate_metamorphic-CLIFF" "mossy_temperate-CLIFF")

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

log "STEP 1-6  Parallel prep + dispatch"
for idx in 0 1 2 3 4 5; do
  prep_and_dispatch_one "$idx" &
done
wait
log "All boxes dispatched"

log "STEP 7  Monitor"
mkdir -p "$OUT_DIR"
declare -A DONE
for idx in 0 1 2 3 4 5; do DONE[$idx]=0; done
while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  all_done=1; status=""
  for idx in 0 1 2 3 4 5; do
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

log "STEP 8  Collecting"
for idx in 0 1 2 3 4 5; do
  ip="${IPS[$idx]}"
  scp -q root@"$ip":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1 || true
done
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
log "$COLLECTED MCAs in $OUT_DIR/"

log "STEP 9  Hash verify"
for f in "$OUT_DIR"/*.mca; do
  [ -f "$f" ] && md5sum "$f"
done

if [ "$INSTALL" = "1" ]; then
  log "STEP 10  Installing to Vandirtest10"
  [ -d "$VANDIRTEST10" ] && cp -f "$OUT_DIR"/*.mca "$VANDIRTEST10/" && log "  installed"
else
  log "STEP 10  Install SKIPPED -- MCAs in $OUT_DIR/."
fi

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
log "[DONE] in ${ELAPSED}m"
echo ""
echo "Teleport coords:"
for idx in 0 1 2 3 4 5; do
  tx="${TXS[$idx]}"; tz="${TZS[$idx]}"; name="${NAMES[$idx]}"
  echo "  /tp @s $((tx*512+256)) 200 $((tz*512+256))   # ($tx,$tz) $name"
done

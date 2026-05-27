#!/usr/bin/env bash
# render_s88.sh — 4-tile render across 4 boxes, S88 mask wiring inspection.
#
# Tile plan:
#   IP1 -> tile (36,15) karst-cliff      ← THE test tile (cap/talus/bedrock)
#   IP2 -> tile (89,52) cliff-litho      ← lithology variety + cliffs
#   IP3 -> tile (24,80) desert-rock      ← sand + aspect-heavy
#   IP4 -> tile (60,69) painted-river    ← river regression check
#
# Usage:
#   bash cloud_bake/render_s88.sh IP1 IP2 IP3 IP4
#
# Env:
#   NO_INSTALL=1 (default) -- collect to output_s88/, no auto-copy to Vandirtest10.
#   INSTALL=1               -- copy MCAs to Vandirtest10 after collect.
#   SKIP_CACHE_CLEAR=1      -- skip the bed/spline cache wipe (only when
#                              you're SURE masks haven't changed).  Default
#                              behavior FORCES cache regen because the 4 new
#                              S88 masks may not be in the box snapshot.
#
# Pulls latest s85-cherry-picks branch on each box, uploads 4 newly-built
# S88 masks (aspect, cliff_cap, talus_apron, bedrock_drainage) + the usual
# override/lithology trio, then dispatches one tile per box in parallel.

set -u

if [ "$#" -ne 4 ]; then
  echo "Usage: $0 IP1 IP2 IP3 IP4"
  echo "  IP1 -> tile (36,15) karst-cliff"
  echo "  IP2 -> tile (89,52) cliff-litho"
  echo "  IP3 -> tile (24,80) desert-rock"
  echo "  IP4 -> tile (60,69) painted-river"
  exit 1
fi

# Tile assignment (parallel-array style)
IPS=("$1" "$2" "$3" "$4")
TXS=(36 89 24 60)
TZS=(15 52 80 69)
NAMES=("karst-cliff" "cliff-litho" "desert-rock" "painted-river")

BRANCH="s85-cherry-picks"
OUT_DIR="output_s88"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
START_TIME=$(date +%s)
SKIP_CACHE_CLEAR="${SKIP_CACHE_CLEAR:-0}"
NO_INSTALL="${NO_INSTALL:-1}"
INSTALL="${INSTALL:-0}"

# Masks to upload to each box.  Local paths (relative to repo root).
UPLOAD_MASKS=(
  "masks/override.tif"
  "masks/lithology.tif"
  "masks/lithology_region.png"
  # S88 terrain-derived masks built by tools/build_terrain_derived.py.
  # These do NOT live in git; must be scp'd up.
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
  # Args: index (0..3)
  local idx="$1"
  local ip="${IPS[$idx]}"
  local tx="${TXS[$idx]}"
  local tz="${TZS[$idx]}"
  local name="${NAMES[$idx]}"
  local tag="[$ip ($tx,$tz) $name]"
  local log_file="render_s88_${tx}_${tz}.log"
  > "$log_file"

  echo "$tag prep start"  | tee -a "$log_file"

  # 1. SSH known_hosts
  ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null
  local hn
  hn=$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$ip" "hostname" 2>&1)
  echo "$tag $ip -> $hn" | tee -a "$log_file"

  # 2. Pull latest s85-cherry-picks
  echo "$tag pulling $BRANCH" | tee -a "$log_file"
  ssh root@"$ip" "cd /root/minecraft-worldgen && \
    git fetch && (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git pull && \
    git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png 2>/dev/null; \
    git log --oneline -3" 2>&1 | tee -a "$log_file"

  # 3. Cache clear (FORCED by default because S88 masks may have changed
  #    the carver's view).
  if [ "$SKIP_CACHE_CLEAR" = "1" ]; then
    echo "$tag cache clear SKIPPED" | tee -a "$log_file"
  else
    echo "$tag clearing caches" | tee -a "$log_file"
    ssh root@"$ip" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl /root/minecraft-worldgen/masks/_bed_v17_cache.pkl 2>/dev/null"
  fi

  # 4. Upload masks (incl. 4 new S88 masks ~195 MB total)
  echo "$tag uploading ${#UPLOAD_MASKS[@]} masks" | tee -a "$log_file"
  for m in "${UPLOAD_MASKS[@]}"; do
    if [ ! -f "$m" ]; then
      echo "$tag MISSING local file: $m -- aborting box" | tee -a "$log_file"
      return 1
    fi
    scp -q "$m" root@"$ip":/root/minecraft-worldgen/"$m" 2>&1 | tee -a "$log_file"
  done

  # 5. Dispatch render in tmux
  echo "$tag dispatching render" | tee -a "$log_file"
  local tx1=$((tx + 1))
  local tz1=$((tz + 1))
  ssh root@"$ip" "cd /root/minecraft-worldgen && rm -rf output /root/render_done /root/render_*.log && tmux kill-session -t render 2>/dev/null; tmux new -d -s render 'source /root/venv/bin/activate; PYTHONUNBUFFERED=1 python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 $tx --tile-x1 $tx1 --tile-z0 $tz --tile-z1 $tz1 > /root/render_${tx}_${tz}.log 2>&1; touch /root/render_done'" 2>&1 | tee -a "$log_file"

  echo "$tag dispatched OK" | tee -a "$log_file"
  return 0
}

# Run all 4 box preps in parallel (so the long mask uploads overlap)
log "STEP 1-4  Parallel prep + dispatch on 4 boxes..."
for idx in 0 1 2 3; do
  prep_and_dispatch_one "$idx" &
done
wait
log "All boxes dispatched"

# Monitor all 4 boxes until done
log "STEP 5  Monitor (30s poll)"
mkdir -p "$OUT_DIR"
declare -A DONE
for idx in 0 1 2 3; do DONE[$idx]=0; done

while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  all_done=1
  status=""
  for idx in 0 1 2 3; do
    if [ "${DONE[$idx]}" = "1" ]; then
      status="${status}  ($((${TXS[$idx]})),$((${TZS[$idx]})))=DONE"
      continue
    fi
    ip="${IPS[$idx]}"
    marker=$(ssh -o ConnectTimeout=5 root@"$ip" "test -f /root/render_done && echo DONE || echo running" 2>/dev/null)
    if [ "$marker" = "DONE" ]; then
      DONE[$idx]=1
      status="${status}  ($((${TXS[$idx]})),$((${TZS[$idx]})))=DONE"
    else
      all_done=0
      mca=$(ssh -o ConnectTimeout=5 root@"$ip" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
      status="${status}  ($((${TXS[$idx]})),$((${TZS[$idx]})))=${mca:-0}mca"
    fi
  done
  echo "  T+${ELAPSED}m:$status"
  [ "$all_done" = "1" ] && break
  sleep 30
done

# Collect all MCAs
log "STEP 6  Collecting MCAs"
for idx in 0 1 2 3; do
  ip="${IPS[$idx]}"
  tx="${TXS[$idx]}"; tz="${TZS[$idx]}"
  scp -q root@"$ip":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1 || true
done
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
log "$COLLECTED MCAs in $OUT_DIR/"

# Verify with md5
log "STEP 7  Hash verify"
for f in "$OUT_DIR"/*.mca; do
  [ -f "$f" ] && md5sum "$f"
done

# Install only if explicitly requested
if [ "$INSTALL" = "1" ]; then
  log "STEP 8  Installing to Vandirtest10"
  if [ -d "$VANDIRTEST10" ]; then
    cp -f "$OUT_DIR"/*.mca "$VANDIRTEST10/"
    log "  installed"
  fi
else
  log "STEP 8  Auto-install SKIPPED -- MCAs in $OUT_DIR/.  Run: cp $OUT_DIR/*.mca '$VANDIRTEST10/' to install."
fi

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
log "[DONE] in ${ELAPSED}m"
echo ""
echo "Teleport coords:"
for idx in 0 1 2 3; do
  tx="${TXS[$idx]}"; tz="${TZS[$idx]}"; name="${NAMES[$idx]}"
  echo "  /tp @s $((tx*512+256)) 200 $((tz*512+256))   # ($tx,$tz) $name"
done

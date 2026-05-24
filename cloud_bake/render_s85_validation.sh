#!/usr/bin/env bash
# render_s85_validation.sh — One-shot S85 validation render on 4 Hetzner CCX63 boxes.
#
# Usage (from Git Bash, project root):
#   cd /c/Users/nicho/minecraft-worldgen
#   bash cloud_bake/render_s85_validation.sh IP1 IP2 IP3 IP4
#
# Prerequisite:
#   - 4 CCX63 boxes already spun from the vandir-baked-s85-validated snapshot
#     (or vandir-baked-s85-veg if you saved that one).
#     If from a fresh Ubuntu image, run bootstrap_master.sh on each box first.
#
# What it does:
#   1. SSH host-key setup + sanity test
#   2. SSH key relay to Box 1 (for inter-box rsync)
#   3. git pull s85-cherry-picks on all 4 boxes
#   4. Clear stale caches (_bed_cache_v17.pkl, _spline_cache.pkl)
#   5. Upload Vegetation/ to Box 1 (~3 min) — skips if already there
#   6. Rsync Vegetation/ from Box 1 -> Boxes 2,3,4 (~3 min)
#   7. Dispatch 36-tile render across 4 boxes (9 tiles each, parallel inside tmux)
#   8. Monitor progress (poll every 60s)
#   9. Collect MCAs to ./output_s85_validation/
#   10. Copy MCAs into Vandirtest10/region/
#
# Total wall: ~30-40 min. Cost: ~$1-2.

set -u  # error on undefined vars (don't use -e — we want to continue on minor SSH glitches)

if [ "$#" -ne 4 ]; then
  echo "Usage: $0 IP1 IP2 IP3 IP4"
  echo ""
  echo "Spin 4x CCX63 from your snapshot in Hetzner Console first, then pass the IPs here."
  exit 1
fi

IP1="$1"; IP2="$2"; IP3="$3"; IP4="$4"
IPS=("$IP1" "$IP2" "$IP3" "$IP4")

# Tile distribution: 9 per box, round-robin.  See cloud_bake/validation_tiles.txt
TILES_BOX1="36,7 89,57 31,5 41,35 33,49 72,92 92,50 18,62 43,89"
TILES_BOX2="19,23 33,13 31,3 14,50 34,9 50,48 86,78 86,75 32,89"
TILES_BOX3="26,10 20,36 38,11 11,64 36,75 8,73 28,7 38,15 40,28"
TILES_BOX4="32,10 80,50 17,66 33,6 17,41 15,61 71,91 89,52 10,77"

VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
BRANCH="s85-cherry-picks"
START_TIME=$(date +%s)

step() {
  local elapsed=$(( ($(date +%s) - START_TIME) / 60 ))
  echo ""
  echo "=========================================="
  echo "[T+${elapsed}m] $*"
  echo "=========================================="
}

# ===== STEP 1: SSH host-key setup =====
step "STEP 1/10  Adding host keys"
for IP in "${IPS[@]}"; do
  ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null
done
echo "  done."

# ===== STEP 2: SSH sanity check =====
step "STEP 2/10  Testing SSH to all 4 boxes"
SSH_OK=0
for IP in "${IPS[@]}"; do
  hn=$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$IP" "hostname" 2>&1)
  echo "  $IP -> $hn"
  if [[ "$hn" == *"denied"* || "$hn" == *"timed out"* ]]; then
    echo "  ERROR: $IP not reachable"
    exit 1
  fi
done

# ===== STEP 3: SSH key relay to Box 1 =====
step "STEP 3/10  Copying laptop SSH key to Box 1 (for inter-box rsync)"
if scp -o BatchMode=yes ~/.ssh/id_ed25519 root@"$IP1":/root/.ssh/id_ed25519 2>/dev/null; then
  ssh root@"$IP1" "chmod 600 /root/.ssh/id_ed25519 && ssh-keyscan -H $IP2 $IP3 $IP4 >> /root/.ssh/known_hosts 2>/dev/null"
  echo "  key uploaded + host keys for boxes 2-4 added."
else
  echo "  WARN: scp of ~/.ssh/id_ed25519 failed; rsync between boxes may prompt for password"
fi

# ===== STEP 4: Pull s85-cherry-picks on all 4 =====
step "STEP 4/10  Pulling branch '$BRANCH' on all 4 boxes"
for IP in "${IPS[@]}"; do
  echo "  --- $IP ---"
  ssh root@"$IP" "cd /root/minecraft-worldgen && \
    rm -f masks/hydro_region.png.bak masks/lithology_region.png.bak masks/override.tif.bak_s84_pre_lrfc_north_strip 2>/dev/null; \
    git fetch && \
    (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git pull && \
    git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png masks/hydro_region.png.bak masks/lithology_region.png.bak masks/override.tif.bak_s84_pre_lrfc_north_strip 2>/dev/null; \
    ls masks/hydro_region.png masks/lithology_region.png 2>&1 | head -2; \
    git log --oneline -1"
done

# ===== STEP 5: Clear stale caches =====
step "STEP 5/10  Clearing stale mask caches"
for IP in "${IPS[@]}"; do
  ssh root@"$IP" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl /root/minecraft-worldgen/masks/_bed_v17_cache.pkl 2>/dev/null; ls /root/minecraft-worldgen/masks/*.pkl 2>/dev/null || echo '  $IP: caches cleared'"
done

# ===== STEP 6: Upload Vegetation/ to Box 1 =====
step "STEP 6/10  Uploading Vegetation/ (30 MB, 998 files) to Box 1"
HAS_VEG=$(ssh root@"$IP1" "ls /root/minecraft-worldgen/Vegetation 2>/dev/null | wc -l")
if [ "$HAS_VEG" -ge 900 ]; then
  echo "  Box 1 already has Vegetation/ ($HAS_VEG files) — skipping upload."
else
  echo "  Box 1 has $HAS_VEG vegetation files. Uploading from laptop (~3 min)..."
  scp -r -q Vegetation/ root@"$IP1":/root/minecraft-worldgen/ 2>&1
  AFTER=$(ssh root@"$IP1" "ls /root/minecraft-worldgen/Vegetation 2>/dev/null | wc -l")
  echo "  Box 1 now has $AFTER vegetation files."
fi

# ===== STEP 7: Rsync Vegetation/ to other boxes =====
step "STEP 7/10  Mirror Vegetation/ from Box 1 -> Boxes 2,3,4"
for IP in $IP2 $IP3 $IP4; do
  HAS_VEG=$(ssh root@"$IP" "ls /root/minecraft-worldgen/Vegetation 2>/dev/null | wc -l")
  if [ "$HAS_VEG" -ge 900 ]; then
    echo "  $IP already has $HAS_VEG vegetation files — skipping rsync."
    continue
  fi
  echo "  --- Rsync to $IP ---"
  ssh root@"$IP1" "rsync -az -e 'ssh -i /root/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new' /root/minecraft-worldgen/Vegetation/ root@$IP:/root/minecraft-worldgen/Vegetation/" 2>&1
  AFTER=$(ssh root@"$IP" "ls /root/minecraft-worldgen/Vegetation 2>/dev/null | wc -l")
  echo "  $IP now has $AFTER files."
done

# ===== STEP 8: Dispatch render =====
step "STEP 8/10  Dispatching 36-tile render (9 tiles/box, parallel inside tmux)"
dispatch() {
  local IP=$1
  local TILES=$2
  ssh root@"$IP" "cd /root/minecraft-worldgen && rm -rf output /root/render_done /root/render_*.log && tmux kill-session -t render 2>/dev/null; tmux new -d -s render 'source /root/venv/bin/activate; for T in $TILES; do X=\${T%,*}; Z=\${T#*,}; PYTHONUNBUFFERED=1 python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 \$X --tile-x1 \$((X+1)) --tile-z0 \$Z --tile-z1 \$((Z+1)) > /root/render_\${X}_\${Z}.log 2>&1 & done; wait; touch /root/render_done'"
  echo "  $IP dispatched (9 tiles)"
}
dispatch "$IP1" "$TILES_BOX1"
dispatch "$IP2" "$TILES_BOX2"
dispatch "$IP3" "$TILES_BOX3"
dispatch "$IP4" "$TILES_BOX4"

# ===== STEP 9: Monitor progress =====
step "STEP 9/10  Monitoring (first tile per box runs cache regen ~10 min; subsequent ~3-5 min each)"
LAST_TOTAL=0
STALL_COUNT=0
while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  echo "--- T+${ELAPSED}m ---"
  TOTAL_MCAS=0
  DONE_COUNT=0
  for IP in "${IPS[@]}"; do
    mca=$(ssh root@"$IP" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
    marker=$(ssh root@"$IP" "test -f /root/render_done && echo DONE || echo running" 2>/dev/null)
    echo "  $IP: $mca MCAs ($marker)"
    TOTAL_MCAS=$((TOTAL_MCAS + mca))
    [ "$marker" = "DONE" ] && DONE_COUNT=$((DONE_COUNT + 1))
  done
  echo "  total: $TOTAL_MCAS / 36 MCAs, $DONE_COUNT / 4 boxes done"
  if [ "$DONE_COUNT" -eq 4 ]; then
    echo "  ALL DONE."
    break
  fi
  # Stall guard: if no MCAs added in 5 consecutive polls (~5 min), warn
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

# ===== STEP 10: Collect + install =====
step "STEP 10/10  Collecting MCAs to laptop + installing into Vandirtest10"
mkdir -p output_s85_validation
for IP in "${IPS[@]}"; do
  echo "  scp from $IP..."
  scp -q root@"$IP":/root/minecraft-worldgen/output/r.*.mca output_s85_validation/ 2>&1
done
COLLECTED=$(ls output_s85_validation/*.mca 2>/dev/null | wc -l)
echo "  collected: $COLLECTED MCAs in ./output_s85_validation/"

if [ -d "$VANDIRTEST10" ]; then
  cp output_s85_validation/*.mca "$VANDIRTEST10/" 2>&1
  FINAL=$(ls "$VANDIRTEST10/" | grep -c "\.mca$")
  echo "  Vandirtest10/region/ now has $FINAL .mca files total"
else
  echo "  WARN: Vandirtest10 region dir not found at $VANDIRTEST10 — copy manually"
fi

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
echo ""
echo "=========================================="
echo "[DONE] S85 validation render complete in ${ELAPSED}m"
echo "  Tiles rendered: $COLLECTED / 36"
echo ""
echo "Next steps:"
echo "  1. (Optional) In Hetzner Console, snapshot Box 1 as 'vandir-baked-s85-veg'"
echo "     - next render skips Vegetation upload entirely if you spin from this"
echo "  2. DELETE all 4 boxes in Hetzner Console (stops billing immediately)"
echo "  3. Quit Minecraft to title screen, re-enter Vandirtest10"
echo "  4. TP through the 36 tiles per cloud_bake/validation_tiles.txt"
echo "=========================================="

#!/usr/bin/env bash
# render_s86_3x3.sh - S86 BT-banding 3x3 validation render on 1 Hetzner CCX63.
#
# Usage:
#   cd /c/Users/nicho/minecraft-worldgen
#   bash cloud_bake/render_s86_3x3.sh CENTER_X CENTER_Z IP
#
# Example:
#   bash cloud_bake/render_s86_3x3.sh 26 10 5.78.184.66
#
# Renders the 3x3 of tiles centered on (CENTER_X, CENTER_Z), i.e. tiles
# (CX-1 .. CX+1) x (CZ-1 .. CZ+1) = 9 tiles.
#
# Prerequisite:
#   - 1 CCX63 spun from vandir-baked-s85-validated (or vandir-baked-s85-veg) snapshot
#   - masks/override_s86_BT_bands.tif exists locally (run apply_BT_banding.py +
#     upscale_override_BT_banded.py first)
#
# What it does:
#   1. SSH host key + sanity check
#   2. git pull s85-cherry-picks on the box
#   3. Clear stale mask caches
#   4. **rsync override_s86_BT_bands.tif up as masks/override.tif (NEW for S86)**
#   5. Upload Vegetation/ if missing (~3 min)
#   6. Dispatch 9-tile render inside tmux
#   7. Monitor (poll every 60s)
#   8. Collect MCAs to ./output_s86_3x3/
#   9. Copy to Vandirtest10/region/
#
# Wall: ~15-25 min depending on tile complexity. Cost: ~$0.20-0.30.

set -u

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 CENTER_X CENTER_Z IP"
  echo ""
  echo "  CENTER_X, CENTER_Z: tile coordinates (0..96)"
  echo "  IP: Hetzner box IP (CCX63 from S85 snapshot)"
  echo ""
  echo "Renders 3x3 = 9 tiles centered on (CX, CZ)."
  exit 1
fi

CX="$1"; CZ="$2"; IP="$3"
BRANCH="s85-cherry-picks"
LOCAL_OVERRIDE="masks/override_s86_BT_bands.tif"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
OUT_DIR="output_s86_3x3"
START_TIME=$(date +%s)

if [ ! -f "$LOCAL_OVERRIDE" ]; then
  echo "ERROR: $LOCAL_OVERRIDE not found. Run apply_BT_banding.py + upscale_override_BT_banded.py first."
  exit 1
fi

# Build tile list: 3x3 around (CX, CZ)
TILES=""
for DX in -1 0 1; do
  for DZ in -1 0 1; do
    X=$((CX + DX))
    Z=$((CZ + DZ))
    if [ $X -lt 0 ] || [ $X -gt 96 ] || [ $Z -lt 0 ] || [ $Z -gt 96 ]; then
      echo "  WARN: tile ($X, $Z) out of bounds (0..96), skipping"
      continue
    fi
    TILES="$TILES $X,$Z"
  done
done
TILES=$(echo $TILES | xargs)
N_TILES=$(echo $TILES | wc -w)

echo "S86 3x3 render plan:"
echo "  center: ($CX, $CZ)"
echo "  box:    $IP"
echo "  tiles:  $TILES ($N_TILES tiles)"
echo "  override: $LOCAL_OVERRIDE (will be uploaded as masks/override.tif)"
echo ""

step() {
  local elapsed=$(( ($(date +%s) - START_TIME) / 60 ))
  echo ""
  echo "=========================================="
  echo "[T+${elapsed}m] $*"
  echo "=========================================="
}

# ===== STEP 1: SSH host key + sanity =====
step "STEP 1/9  SSH host key + sanity"
ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null
hn=$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$IP" "hostname" 2>&1)
echo "  $IP -> $hn"
if [[ "$hn" == *"denied"* || "$hn" == *"timed out"* ]]; then
  echo "  ERROR: $IP not reachable"
  exit 1
fi

# ===== STEP 2: Pull branch =====
step "STEP 2/9  Pulling branch '$BRANCH'"
ssh root@"$IP" "cd /root/minecraft-worldgen && \
  rm -f masks/hydro_region.png.bak masks/lithology_region.png.bak masks/override.tif.bak_s84_pre_lrfc_north_strip 2>/dev/null; \
  git fetch && \
  (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
  git pull && \
  git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png masks/hydro_region.png.bak masks/lithology_region.png.bak masks/override.tif.bak_s84_pre_lrfc_north_strip 2>/dev/null; \
  ls masks/hydro_region.png masks/lithology_region.png 2>&1 | head -2; \
  git log --oneline -1"

# ===== STEP 3: Clear stale caches =====
step "STEP 3/9  Clearing stale mask caches"
ssh root@"$IP" "rm -f /root/minecraft-worldgen/masks/_bed_cache_v17.pkl /root/minecraft-worldgen/masks/_spline_cache.pkl /root/minecraft-worldgen/masks/_bed_v17_cache.pkl 2>/dev/null; ls /root/minecraft-worldgen/masks/*.pkl 2>/dev/null || echo '  caches cleared'"

# ===== STEP 4: Upload new override =====
step "STEP 4/9  Uploading $LOCAL_OVERRIDE -> masks/override.tif (new for S86)"
ssh root@"$IP" "cp /root/minecraft-worldgen/masks/override.tif /root/minecraft-worldgen/masks/override.tif.pre_s86 2>/dev/null && echo '  backed up old override on box'"
rsync -avz --progress "$LOCAL_OVERRIDE" root@"$IP":/root/minecraft-worldgen/masks/override.tif
ssh root@"$IP" "ls -la /root/minecraft-worldgen/masks/override.tif && python3 -c 'import rasterio; src=rasterio.open(\"/root/minecraft-worldgen/masks/override.tif\"); print(\"  shape:\", src.shape, \"dtype:\", src.dtypes[0])'"

# ===== STEP 5: Vegetation upload if missing =====
step "STEP 5/9  Verifying Vegetation/ on box"
HAS_VEG=$(ssh root@"$IP" "ls /root/minecraft-worldgen/Vegetation 2>/dev/null | wc -l")
if [ "$HAS_VEG" -ge 900 ]; then
  echo "  box already has Vegetation/ ($HAS_VEG files) - skipping upload."
else
  echo "  box has $HAS_VEG vegetation files. Uploading from laptop (~3 min)..."
  scp -r -q Vegetation/ root@"$IP":/root/minecraft-worldgen/ 2>&1
  AFTER=$(ssh root@"$IP" "ls /root/minecraft-worldgen/Vegetation 2>/dev/null | wc -l")
  echo "  box now has $AFTER vegetation files."
fi

# ===== STEP 6: Dispatch render =====
step "STEP 6/9  Dispatching $N_TILES-tile render"
ssh root@"$IP" "cd /root/minecraft-worldgen && rm -rf output /root/render_done /root/render_*.log && tmux kill-session -t render 2>/dev/null; tmux new -d -s render 'source /root/venv/bin/activate; for T in $TILES; do X=\${T%,*}; Z=\${T#*,}; PYTHONUNBUFFERED=1 python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 \$X --tile-x1 \$((X+1)) --tile-z0 \$Z --tile-z1 \$((Z+1)) > /root/render_\${X}_\${Z}.log 2>&1 & done; wait; touch /root/render_done'"
echo "  dispatched."

# ===== STEP 7: Monitor =====
step "STEP 7/9  Monitoring (first tile runs cache regen ~10 min; subsequent ~3-5 min)"
LAST_TOTAL=0
STALL_COUNT=0
while true; do
  ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
  mca=$(ssh root@"$IP" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
  marker=$(ssh root@"$IP" "test -f /root/render_done && echo DONE || echo running" 2>/dev/null)
  echo "--- T+${ELAPSED}m --- $mca / $N_TILES MCAs ($marker)"
  if [ "$marker" = "DONE" ]; then
    echo "  ALL DONE."
    break
  fi
  if [ "$mca" -eq "$LAST_TOTAL" ]; then
    STALL_COUNT=$((STALL_COUNT + 1))
    if [ "$STALL_COUNT" -ge 10 ]; then
      echo "  WARN: no MCAs produced in ~10 min. Check render_*.log on the box."
    fi
  else
    STALL_COUNT=0
  fi
  LAST_TOTAL=$mca
  sleep 60
done

# ===== STEP 8: Collect =====
step "STEP 8/9  Collecting MCAs to laptop"
mkdir -p "$OUT_DIR"
scp -q root@"$IP":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>&1
COLLECTED=$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l)
echo "  collected: $COLLECTED MCAs in ./$OUT_DIR/"

# ===== STEP 9: DO NOT auto-install =====
# User asked to NOT auto-copy MCAs into Vandirtest10 because they're walking the
# existing 36-tile S85 render right now and a copy would clobber their world state.
step "STEP 9/9  MCAs left in $OUT_DIR (NOT auto-copied to Vandirtest10)"
echo "  $COLLECTED MCAs ready at: $(pwd)/$OUT_DIR/"
echo ""
echo "When you're done with the S85 walk and ready to view the S86 3x3:"
echo "  cp $OUT_DIR/*.mca '$VANDIRTEST10/'"

ELAPSED=$(( ($(date +%s) - START_TIME) / 60 ))
echo ""
echo "=========================================="
echo "[DONE] S86 3x3 render complete in ${ELAPSED}m"
echo "  Tiles rendered: $COLLECTED / $N_TILES"
echo ""
echo "Next: Quit MC to title, re-enter Vandirtest10, TP around center ($CX, $CZ)."
echo "  Tile (X, Z) -> world coords approx ((X*512), 100, (Z*512))"
echo "  Center: /tp @s $((CX*512+256)) 200 $((CZ*512+256))"
echo "=========================================="
echo ""
echo "If render looks good, swap in the new override LOCALLY:"
echo "  mv masks/override.tif masks/override_pre_s86.tif"
echo "  mv masks/override_s86_BT_bands.tif masks/override.tif"
echo ""
echo "If render looks bad, do nothing locally - rerun apply_BT_banding.py with"
echo "different thresholds and try again."

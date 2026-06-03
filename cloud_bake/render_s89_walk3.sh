#!/usr/bin/env bash
# render_s89_walk3.sh — S89 walk-3 targeted re-render (15 tiles).
#
# Only tiles relevant to the walk-3 change batch: the 6 litho test tiles + the
# called-out biomes + tiles touched by pending biome changes.
#   - deep-strata + TIER-SPECIFIC ribs + rock edge-stroke talus-gap fix (6 rock)
#   - krummholz SPARSE fix (rock_temperate_basaltic, SBT, deepslate)
#   - ARCTIC_TUNDRA<625 -> SNOWY_BOREAL_TAIGA remap (ARCTIC + rock peaks)
#   - BOREAL_ALPINE talus/rock fade fix + more short grass
#   - BIRCH_FOREST density (canopy radius 0.55 + BASE 1.0)
#   - SEMI_ARID fewer bushes + more living short grass
#   - KARST rare gravel salt-and-pepper
#   - SCRUBBY smaller surface blobs + more grass
#   - tree rotation variety + slight density (BOREAL_TAIGA, MIXED_FOREST, BIRCH)
# Changes are config + runtime code only — NO mask geometry changed — but the
# on-box build is kept because rock_layers/snow_physics masks aren't in the snap.
#
# Usage: bash cloud_bake/render_s89_walk3.sh IP1 [IP2 ...]
#   Tiles round-robin across however many IPs you pass. 15 tiles / 8 boxes
#   = ~2 tiles each. Spin from the baked snapshot (needs masks/height.tif,
#   flow.tif, lithology.tif present — they come with the snapshot).
#
# Env: INSTALL=0 skip the Vandirtest10 copy (MCAs left in OUT_DIR).
#
# Timing: ~5 min mask build (scale 8) + ~6-7 min per tile render.
#   8 boxes (~2 tiles each) ~= 16-20 min wall.

set -u
[ "$#" -ge 1 ] || { echo "Usage: $0 IP1 [IP2 ...]"; exit 1; }
IPS=("$@")
NB=${#IPS[@]}

# 15 walk-3 tiles: "tx:tz:label". 6 litho test tiles + called-out biomes +
# pending-biome-change tiles.
TILES=(
  "31:21:rock_deepslate_metamorphic"
  "74:66:rock_granitic"
  "33:18:rock_limestone"
  "29:20:rock_arid_basaltic"
  "29:12:rock_temperate_basaltic"
  "72:68:rock_mossy_temperate"
  "32:13:ARCTIC_TUNDRA"
  "30:10:SNOWY_BOREAL_TAIGA"
  "21:23:BOREAL_ALPINE"
  "60:41:BIRCH_FOREST"
  "27:65:SEMI_ARID_SHRUBLAND"
  "34:9:KARST_BARRENS"
  "85:79:SCRUBBY_HEATHLAND"
  "64:54:BOREAL_TAIGA"
  "50:50:MIXED_FOREST"
)

# Precise land TP commands (from memory/biome_reference_tiles.csv). These land
# you ABOVE real ground.
TP_LIST=(
  "rock_deepslate_metamorphic  /tp @s 16228 734 11068   # ribs(mid=light), edge talus-gap fix, krummholz, tundra->SBT"
  "rock_granitic               /tp @s 38028 717 33940   # ribs(mid=light), edge stroke"
  "rock_limestone              /tp @s 17052 712 9436    # ribs(mid=dark), edge stroke"
  "rock_arid_basaltic          /tp @s 15108 692 10700   # ribs(mid=light), edge stroke"
  "rock_temperate_basaltic     /tp @s 15276 683 6580    # KRUMMHOLZ sparse fix, ribs(mid=dark), strata"
  "rock_mossy_temperate        /tp @s 37284 361 35012   # ribs(mid=dark), edge stroke"
  "ARCTIC_TUNDRA               /tp @s 16636 670 6908    # tundra<625 -> SBT remap, snow edge stroke"
  "SNOWY_BOREAL_TAIGA          /tp @s 15612 525 5372    # krummholz sparse, no trees-on-snow (reverted)"
  "BOREAL_ALPINE               /tp @s 11004 147 12028   # talus/rock fade gap fix + more short grass"
  "BIRCH_FOREST                /tp @s 30972 116 21244   # DENSITY fix (radius 0.55 + BASE 1.0)"
  "SEMI_ARID_SHRUBLAND         /tp @s 14076 111 33532   # fewer bushes (0.22) + more living short grass"
  "KARST_BARRENS               /tp @s 17660 362 4860    # RARE gravel salt-and-pepper (0.05)"
  "SCRUBBY_HEATHLAND           /tp @s 43772 123 40700   # smaller surface blobs + more grass"
  "BOREAL_TAIGA                /tp @s 33020 219 27900   # rotation variety + density"
  "MIXED_FOREST                /tp @s 25852 112 25852   # rotation variety + density"
)

BRANCH="s85-cherry-picks"
OUT_DIR="output_s89_walk3"
VANDIRTEST10="/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
INSTALL="${INSTALL:-1}"
START_TIME=$(date +%s)

UPLOAD_MASKS=(
  "masks/override.tif"
  "masks/lithology.tif"
  "masks/lithology_region.png"
)

# Round-robin the tiles onto boxes.
declare -a BOX_TILES
for b in "${!IPS[@]}"; do BOX_TILES[$b]=""; done
for i in "${!TILES[@]}"; do
  b=$(( i % NB ))
  BOX_TILES[$b]="${BOX_TILES[$b]}${BOX_TILES[$b]:+,}${TILES[$i]}"
done

log() { echo "[T+$(( ($(date +%s) - START_TIME) / 60 ))m] $*"; }

prep_dispatch() {
  local b="$1" ip="${IPS[$b]}" list="${BOX_TILES[$b]}"
  local lf="render_s89_walk3_${b}.log"; > "$lf"
  echo "[$ip] tiles: $list" | tee -a "$lf"
  ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null

  # 1. pull code. HARD RESET to origin -- the flag-assert below dirties
  # config/thresholds.json, which would make a plain `git pull` refuse on the
  # next render. reset --hard discards that box-local edit so the box always
  # lands exactly on origin/$BRANCH.
  ssh root@"$ip" "cd /root/minecraft-worldgen && git fetch origin && \
    (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git reset --hard origin/$BRANCH && \
    git log --oneline -2" 2>&1 | tee -a "$lf"

  # 2. assert feature flags box-locally (no-op now they're committed ON; kept as
  # a safety net for boxes on an older commit). NOT committed.
  ssh root@"$ip" "cd /root/minecraft-worldgen && python3 -" <<'PYEOF' 2>&1 | tee -a "$lf"
import json
p = "config/thresholds.json"
d = json.load(open(p))
d["lithology"]["rock_layers"]["enabled"] = True
d["snow_physics"]["enabled"] = True
json.dump(d, open(p, "w"), indent=2)
print("flags ON: lithology.rock_layers + snow_physics")
PYEOF

  # 3. upload source masks (in case the box copy is stale)
  for m in "${UPLOAD_MASKS[@]}"; do
    [ -f "$m" ] || { echo "[$ip] MISSING $m"; return 1; }
    scp -q "$m" root@"$ip":/root/minecraft-worldgen/"$m" 2>&1 | tee -a "$lf"
  done

  # 4. dispatch (in tmux, survives ssh drop): build 4 masks -> render tiles -> done
  local tilecmds=""
  IFS=',' read -ra TT <<< "$list"
  for t in "${TT[@]}"; do
    local tx="${t%%:*}" rest="${t#*:}"; local tz="${rest%%:*}"
    local tx1=$((tx + 1)) tz1=$((tz + 1))
    tilecmds+="echo \"[render] ($tx,$tz) start\" >> /root/render_build.log; python3 run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 $tx --tile-x1 $tx1 --tile-z0 $tz --tile-z1 $tz1 >> /root/render_${tx}_${tz}.log 2>&1; "
  done

  local cmd="cd /root/minecraft-worldgen && rm -f /root/render_done && rm -rf output /root/render_*.log && tmux kill-session -t r89w3 2>/dev/null; "
  cmd+="tmux new -d -s r89w3 'source /root/venv/bin/activate; export PYTHONUNBUFFERED=1; "
  cmd+="echo BUILD_START > /root/render_build.log; "
  cmd+="python3 tools/build_terrain_derived.py --only rock_layers,talus,cap --scale 8 >> /root/render_build.log 2>&1; "
  cmd+="python3 tools/build_snow_physics.py --scale 8 >> /root/render_build.log 2>&1; "
  cmd+="echo BUILD_DONE >> /root/render_build.log; "
  cmd+="$tilecmds"
  cmd+="touch /root/render_done'"

  ssh root@"$ip" "$cmd" 2>&1 | tee -a "$lf"
  echo "[$ip] dispatched" | tee -a "$lf"
}

log "Dispatch $((${#TILES[@]})) tiles across $NB box(es)"
for b in "${!IPS[@]}"; do prep_dispatch "$b" & done
wait
log "All dispatched"

log "Monitor (build then render; poll 30s)"
mkdir -p "$OUT_DIR"
declare -A DONE; for b in "${!IPS[@]}"; do DONE[$b]=0; done
while true; do
  all=1; st=""
  for b in "${!IPS[@]}"; do
    if [ "${DONE[$b]}" = "1" ]; then st="$st  b$b=DONE"; continue; fi
    ip="${IPS[$b]}"
    m=$(ssh -o ConnectTimeout=5 root@"$ip" "test -f /root/render_done && echo DONE || echo run" 2>/dev/null)
    if [ "$m" = "DONE" ]; then DONE[$b]=1; st="$st  b$b=DONE"; else
      all=0
      phase=$(ssh -o ConnectTimeout=5 root@"$ip" "tail -1 /root/render_build.log 2>/dev/null" 2>/dev/null)
      mca=$(ssh -o ConnectTimeout=5 root@"$ip" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
      st="$st  b$b=${mca:-0}mca[${phase:-?}]"
    fi
  done
  log "$st"
  [ "$all" = "1" ] && break
  sleep 30
done

log "Collect"
for b in "${!IPS[@]}"; do
  scp -q root@"${IPS[$b]}":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>/dev/null || true
done
log "$(ls "$OUT_DIR"/*.mca 2>/dev/null | wc -l) MCAs collected; md5:"
for f in "$OUT_DIR"/*.mca; do [ -f "$f" ] && md5sum "$f"; done

if [ "$INSTALL" = "1" ] && [ -d "$VANDIRTEST10" ]; then
  cp -f "$OUT_DIR"/*.mca "$VANDIRTEST10/" && log "installed to Vandirtest10"
else
  log "install skipped (INSTALL=$INSTALL); MCAs in $OUT_DIR/"
fi

log "DONE in $(( ($(date +%s) - START_TIME) / 60 ))m"
echo ""
echo "=== Validation TP commands (FULLY QUIT + reopen MC first) ==="
for entry in "${TP_LIST[@]}"; do echo "  $entry"; done

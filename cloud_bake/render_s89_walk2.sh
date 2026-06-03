#!/usr/bin/env bash
# render_s89_walk2.sh — S89 walk-2 targeted re-render (19 tiles).
#
# Only the tiles that demonstrate the walk-2 change batch:
#   - deep-strata palette change (6 rock tiles, one per litho group)
#   - rock-gap edge stroke + snow edge stroke (rock tiles + ARCTIC_TUNDRA)
#   - SBT-over-snow on gentle slopes + taiga treeline 700 (SBT/BT)
#   - BOREAL_ALPINE way-more-short-grass
#   - density bumps (BIRCH, RAINFOREST_COAST, DRY_OAK_SAVANNA) + bush ABS
#     (KARST, DESERT_STEPPE, SEMI_ARID, MAQUIS, SCRUBBY)
#   - KARST gravel salt-and-pepper (white noise)
#   - river seagrass speckle (RIPARIAN_WOODLAND)
# Changes are config + runtime code only — NO mask geometry changed — but the
# on-box build is kept because rock_layers/snow_physics masks aren't in the snap.
#
# Usage: bash cloud_bake/render_s89_walk2.sh IP1 [IP2 ...]
#   Tiles round-robin across however many IPs you pass. 19 tiles / 8 boxes
#   = 2-3 tiles each. Spin from the baked snapshot (needs masks/height.tif,
#   flow.tif, lithology.tif present — they come with the snapshot).
#
# Env: INSTALL=0 skip the Vandirtest10 copy (MCAs left in OUT_DIR).
#
# Timing: ~5 min mask build (scale 8) + ~6-7 min per tile render.
#   8 boxes (2-3 tiles each) ~= 22-26 min wall.

set -u
[ "$#" -ge 1 ] || { echo "Usage: $0 IP1 [IP2 ...]"; exit 1; }
IPS=("$@")
NB=${#IPS[@]}

# 19 walk-2 tiles: "tx:tz:label". Rock tiles first (strata + edge stroke), then
# snow/taiga, then density/bush/karst, then river.
TILES=(
  "31:21:rock_deepslate_metamorphic"
  "74:66:rock_granitic"
  "33:18:rock_limestone"
  "29:20:rock_arid_basaltic"
  "29:12:rock_temperate_basaltic"
  "72:68:rock_mossy_temperate"
  "32:13:ARCTIC_TUNDRA"
  "30:10:SNOWY_BOREAL_TAIGA"
  "64:54:BOREAL_TAIGA"
  "21:23:BOREAL_ALPINE"
  "60:41:BIRCH_FOREST"
  "8:67:RAINFOREST_COAST"
  "29:76:DRY_OAK_SAVANNA"
  "34:9:KARST_BARRENS"
  "19:63:DESERT_STEPPE_TRANSITION"
  "27:65:SEMI_ARID_SHRUBLAND"
  "30:90:DRY_WOODLAND_MAQUIS"
  "85:79:SCRUBBY_HEATHLAND"
  "80:50:RIPARIAN_WOODLAND"
)

# Precise land TP commands (from memory/biome_reference_tiles.csv). These land
# you ABOVE real ground — do NOT use tile-center (drops into ocean on partial
# coastal tiles like RIPARIAN).
TP_LIST=(
  "rock_deepslate_metamorphic  /tp @s 16228 734 11068   # strata stone/tuff, rock+snow edge stroke"
  "rock_granitic               /tp @s 38028 717 33940   # strata granite/dripstone, rock edge stroke"
  "rock_limestone              /tp @s 17052 712 9436    # strata diorite/calcite, rock edge stroke"
  "rock_arid_basaltic          /tp @s 15108 692 10700   # strata basalt mix, rock edge stroke"
  "rock_temperate_basaltic     /tp @s 15276 683 6580    # strata deepslate/cobbled, rock edge stroke"
  "rock_mossy_temperate        /tp @s 37284 361 35012   # strata stone/mossy_cobble, rock edge stroke"
  "ARCTIC_TUNDRA               /tp @s 16636 670 6908    # snow edge stroke (high snow)"
  "SNOWY_BOREAL_TAIGA          /tp @s 15612 525 5372    # SBT-over-snow on low slopes, treeline 700"
  "BOREAL_TAIGA                /tp @s 33020 219 27900   # treeline 700 (low terrain — may be subtle)"
  "BOREAL_ALPINE               /tp @s 11004 147 12028   # way more short grass"
  "BIRCH_FOREST                /tp @s 30972 116 21244   # density up (0.65->0.85)"
  "RAINFOREST_COAST            /tp @s 4348 123 34556    # density up (0.32->0.80)"
  "DRY_OAK_SAVANNA             /tp @s 15100 133 39172   # tree density up (0.15->0.25)"
  "KARST_BARRENS               /tp @s 17660 362 4860    # bush ABS 0.70 + gravel salt-and-pepper"
  "DESERT_STEPPE_TRANSITION    /tp @s 9980 137 32508    # bush ABS 0.65 (~karst), trees unchanged"
  "SEMI_ARID_SHRUBLAND         /tp @s 14076 111 33532   # bush ABS 0.45"
  "DRY_WOODLAND_MAQUIS         /tp @s 15612 113 46332   # bush ABS 0.50 (generic)"
  "SCRUBBY_HEATHLAND           /tp @s 43772 123 40700   # bush ABS 0.55 (~karst)"
  "RIPARIAN_WOODLAND           /tp @s 41316 113 25836   # river seagrass speckle"
)

BRANCH="s85-cherry-picks"
OUT_DIR="output_s89_walk2"
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
  local lf="render_s89_walk2_${b}.log"; > "$lf"
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

  local cmd="cd /root/minecraft-worldgen && rm -f /root/render_done && rm -rf output /root/render_*.log && tmux kill-session -t r89w2 2>/dev/null; "
  cmd+="tmux new -d -s r89w2 'source /root/venv/bin/activate; export PYTHONUNBUFFERED=1; "
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

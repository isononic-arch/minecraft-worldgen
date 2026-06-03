#!/usr/bin/env bash
# render_s89_sweep.sh — S89 FULL-BIOME validation sweep (32 tiles).
#
# 26 clean land biome tiles + 6 mountain/rock tiles (one per lithology group),
# generated land-aware by tools/diag_biome_sampler.py (see
# memory/S89_validation_sweep.md). Renders the full S89 rock + snow + vegetation
# stack (flags are committed ON; the box-local flag-flip below is a no-op kept
# only as a safety net for boxes sitting on an older commit).
#
# On each box: pull branch -> (re)assert flags -> BUILD the 4 scale-8 masks
# (rock_layers, talus, cliff_cap, snow_physics) -> render its assigned tiles ->
# collect + md5 + install to Vandirtest10.
#
# Usage: bash cloud_bake/render_s89_sweep.sh IP1 [IP2 ...]
#   Tiles round-robin across however many IPs you pass. Designed for 8 CCX63
#   boxes (4 tiles each). Spin from the baked snapshot (needs masks/height.tif,
#   flow.tif, lithology.tif present — they come with the snapshot).
#
# Env: INSTALL=0 skip the Vandirtest10 copy (MCAs left in OUT_DIR).
#
# Timing: ~5 min mask build (scale 8) + ~6-7 min per tile render.
#   8 boxes (4 tiles each) ~= 30-35 min wall.

set -u
[ "$#" -ge 1 ] || { echo "Usage: $0 IP1 [IP2 ...]"; exit 1; }
IPS=("$@")
NB=${#IPS[@]}

# 32 sweep tiles: "tx:tz:label"  (26 biome + 6 rock). Order = roughly by region
# so round-robin keeps each box's tiles somewhat clustered.
TILES=(
  "37:8:COASTAL_HEATH"
  "23:29:TEMPERATE_RAINFOREST"
  "64:54:BOREAL_TAIGA"
  "30:10:SNOWY_BOREAL_TAIGA"
  "21:23:BOREAL_ALPINE"
  "32:13:ARCTIC_TUNDRA"
  "33:6:FROZEN_FLATS"
  "32:31:TEMPERATE_DECIDUOUS"
  "8:67:RAINFOREST_COAST"
  "80:50:RIPARIAN_WOODLAND"
  "29:76:DRY_OAK_SAVANNA"
  "34:9:KARST_BARRENS"
  "60:41:BIRCH_FOREST"
  "28:35:EASTERN_TEMPERATE_COAST"
  "50:50:MIXED_FOREST"
  "39:23:CONTINENTAL_STEPPE"
  "30:49:DRY_PINE_BARRENS"
  "85:79:SCRUBBY_HEATHLAND"
  "6:68:LUSH_RAINFOREST_COAST"
  "18:66:SAND_DUNE_DESERT"
  "19:63:DESERT_STEPPE_TRANSITION"
  "27:65:SEMI_ARID_SHRUBLAND"
  "30:90:DRY_WOODLAND_MAQUIS"
  "31:89:TIDAL_JUNGLE_FRINGE"
  "30:86:MANGROVE_COAST"
  "8:73:FRESHWATER_FEN"
  "31:21:rock_deepslate_metamorphic"
  "74:66:rock_granitic"
  "33:18:rock_limestone"
  "29:20:rock_arid_basaltic"
  "29:12:rock_temperate_basaltic"
  "72:68:rock_mossy_temperate"
)

# Precise land TP commands (from memory/biome_reference_tiles.csv). These land
# you ABOVE real ground — do NOT use tile-center, which drops into ocean on the
# partial coastal tiles (MANGROVE/ETC/RIPARIAN/FEN).
TP_LIST=(
  "COASTAL_HEATH               /tp @s 19196 121 4348"
  "TEMPERATE_RAINFOREST        /tp @s 12028 111 15100"
  "BOREAL_TAIGA                /tp @s 33020 219 27900"
  "SNOWY_BOREAL_TAIGA          /tp @s 15612 525 5372"
  "BOREAL_ALPINE               /tp @s 11004 147 12028"
  "ARCTIC_TUNDRA               /tp @s 16636 670 6908"
  "FROZEN_FLATS                /tp @s 17132 132 3308"
  "TEMPERATE_DECIDUOUS         /tp @s 16636 120 16124"
  "RAINFOREST_COAST            /tp @s 4348 123 34556"
  "RIPARIAN_WOODLAND           /tp @s 41316 113 25836"
  "DRY_OAK_SAVANNA             /tp @s 15100 133 39172"
  "KARST_BARRENS               /tp @s 17660 362 4860"
  "BIRCH_FOREST                /tp @s 30972 116 21244"
  "EASTERN_TEMPERATE_COAST     /tp @s 14588 106 18180"
  "MIXED_FOREST                /tp @s 25852 112 25852"
  "CONTINENTAL_STEPPE          /tp @s 20220 149 12028"
  "DRY_PINE_BARRENS            /tp @s 15612 141 25340"
  "SCRUBBY_HEATHLAND           /tp @s 43772 123 40700"
  "LUSH_RAINFOREST_COAST       /tp @s 3324 110 35068"
  "SAND_DUNE_DESERT            /tp @s 9468 154 34044"
  "DESERT_STEPPE_TRANSITION    /tp @s 9980 137 32508"
  "SEMI_ARID_SHRUBLAND         /tp @s 14076 111 33532"
  "DRY_WOODLAND_MAQUIS         /tp @s 15612 113 46332"
  "TIDAL_JUNGLE_FRINGE         /tp @s 16116 131 45868"
  "MANGROVE_COAST              /tp @s 15692 109 44372"
  "FRESHWATER_FEN              /tp @s 4356 122 37644"
  "rock_deepslate_metamorphic  /tp @s 16228 734 11068"
  "rock_granitic               /tp @s 38028 717 33940"
  "rock_limestone              /tp @s 17052 712 9436"
  "rock_arid_basaltic          /tp @s 15108 692 10700"
  "rock_temperate_basaltic     /tp @s 15276 683 6580"
  "rock_mossy_temperate        /tp @s 37284 361 35012"
)

BRANCH="s85-cherry-picks"
OUT_DIR="output_s89_sweep"
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
  local lf="render_s89_sweep_${b}.log"; > "$lf"
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

  local cmd="cd /root/minecraft-worldgen && rm -f /root/render_done && rm -rf output /root/render_*.log && tmux kill-session -t r89sw 2>/dev/null; "
  cmd+="tmux new -d -s r89sw 'source /root/venv/bin/activate; export PYTHONUNBUFFERED=1; "
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

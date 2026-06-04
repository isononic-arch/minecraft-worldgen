#!/usr/bin/env bash
# render_s89_walk5.sh — S89 walk-5 VERIFY render (8 tiles).
#
# 6 OG-litho tiles (all still on OLD aggressive erosion) to confirm the FLOATING-
# BRIDGE / seam / tone-down fixes (827c1bf river-fade, a59592d wide edge-fade,
# 45667d8 tone-down) -- limestone (36,15) is THE bridge tile -- plus birch
# (60,41) and rainforest-coast (8,67) for walk-3p:
#   - flow erosion: river-fade + wide(16) rock edge-fade => NO floating bridge;
#     max_incise 6, smooth 3, face scoop off (planar cliffs left alone)
#   - krummholz: bushes now thin like trees in the zone
#   - BIRCH: fence-sticks rare, full leafy sbirch dominant
#   - RAINFOREST_COAST: tree density toned back (radius 0.9)
#
# Usage: bash cloud_bake/render_s89_walk5.sh IP1 [IP2 ...]
#   8 tiles round-robin. 6 boxes = 1-2/box. Spin from the baked snapshot.
#
# Env: INSTALL=0 skip the Vandirtest10 copy (MCAs left in OUT_DIR).
#
# Timing: ~5 min mask build (scale 8) + ~6-7 min/tile. 6-8 boxes ~= 14-18 min.

set -u
[ "$#" -ge 1 ] || { echo "Usage: $0 IP1 [IP2 ...]"; exit 1; }
IPS=("$@")
NB=${#IPS[@]}

# 8 walk-5 verify tiles: "tx:tz:label". 6 OG-litho (incl limestone bridge tile)
# + birch + rainforest-coast.
TILES=(
  "36:15:limestone_OG"
  "72:60:granitic_OG"
  "24:80:arid_basaltic_OG"
  "89:52:temperate_basaltic_OG"
  "19:44:deepslate_OG"
  "64:72:mossy_temperate_OG"
  "60:41:BIRCH_FOREST"
  "8:67:RAINFOREST_COAST"
)

# Precise land TP commands. OG-litho tiles spawn at Y300 -- fly down to terrain.
TP_LIST=(
  "limestone_OG          /tp @s 18688 300 7936    # THE bridge tile: confirm floating-bridge GONE + clean gullies"
  "granitic_OG           /tp @s 37120 300 30976   # erosion fixes"
  "arid_basaltic_OG      /tp @s 12796 522 41340   # erosion fixes (was jagged red)"
  "temperate_basaltic_OG /tp @s 45572 587 26796   # erosion fixes (grey planar cliff)"
  "deepslate_OG          /tp @s 9984 300 22784    # erosion fixes"
  "mossy_temperate_OG    /tp @s 33024 300 37120   # erosion fixes"
  "BIRCH_FOREST          /tp @s 30972 116 21244   # fence-sticks rare + full sbirch dominant + lush GC"
  "RAINFOREST_COAST      /tp @s 4348 123 34556    # tree density toned back (radius 0.9) + lush GC"
)

BRANCH="s85-cherry-picks"
OUT_DIR="output_s89_walk5"
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
  local lf="render_s89_walk5_${b}.log"; > "$lf"
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

  local cmd="cd /root/minecraft-worldgen && rm -f /root/render_done && rm -rf output /root/render_*.log && tmux kill-session -t r89w5 2>/dev/null; "
  cmd+="tmux new -d -s r89w5 'source /root/venv/bin/activate; export PYTHONUNBUFFERED=1; "
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

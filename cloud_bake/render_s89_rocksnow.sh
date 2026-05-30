#!/usr/bin/env bash
# render_s89_rocksnow.sh — S89 rock_layers + talus + cliff_cap + physics snow.
#
# On each box: pull branch -> flip feature flags (box-local) -> BUILD the 4 new
# masks (rock_layers, talus_apron, cliff_cap, snow_gap_physics) -> render its
# assigned tiles -> collect + md5 + install to Vandirtest10.
#
# Usage: bash cloud_bake/render_s89_rocksnow.sh IP1 [IP2 ...]
#   The 6 pure-litho tiles are round-robin assigned across however many IPs you
#   pass. Spin 1-6 CCX63 boxes from the baked snapshot (needs masks/height.tif,
#   flow.tif, lithology.tif present — they come with the snapshot).
#
# Env: INSTALL=0 skip the Vandirtest10 copy (MCAs left in OUT_DIR).
#
# Timing per box: ~4-6 min mask build (scale 8) + ~6-7 min per tile render.
# 6 boxes (1 tile each) ~= 12-14 min wall.

set -u
[ "$#" -ge 1 ] || { echo "Usage: $0 IP1 [IP2 ...]"; exit 1; }
IPS=("$@")
NB=${#IPS[@]}

# 6 pure-litho / alpine tiles: "tx:tz:name"
TILES=(
  "72:60:granitic"
  "24:80:arid_basaltic"
  "89:52:temperate_basaltic"
  "36:15:limestone"
  "19:44:deepslate_metamorphic"
  "64:72:mossy_temperate"
)

BRANCH="s85-cherry-picks"
OUT_DIR="output_s89_rocksnow"
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
  local lf="render_s89_${b}.log"; > "$lf"
  echo "[$ip] tiles: $list" | tee -a "$lf"
  ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null

  # 1. pull code
  ssh root@"$ip" "cd /root/minecraft-worldgen && git fetch && \
    (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && git pull && \
    git checkout HEAD -- masks/hydro_region.png masks/lithology_region.png 2>/dev/null; \
    git log --oneline -2" 2>&1 | tee -a "$lf"

  # 2. flip feature flags box-locally (NOT committed)
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

  local cmd="cd /root/minecraft-worldgen && rm -f /root/render_done && rm -rf output /root/render_*.log && tmux kill-session -t r89 2>/dev/null; "
  cmd+="tmux new -d -s r89 'source /root/venv/bin/activate; export PYTHONUNBUFFERED=1; "
  cmd+="echo BUILD_START > /root/render_build.log; "
  cmd+="python3 tools/build_terrain_derived.py --only rock_layers,talus,cap --scale 8 >> /root/render_build.log 2>&1; "
  cmd+="python3 tools/build_snow_physics.py --scale 8 >> /root/render_build.log 2>&1; "
  cmd+="echo BUILD_DONE >> /root/render_build.log; "
  cmd+="$tilecmds"
  cmd+="touch /root/render_done'"

  ssh root@"$ip" "$cmd" 2>&1 | tee -a "$lf"
  echo "[$ip] dispatched" | tee -a "$lf"
}

log "Dispatch across $NB box(es)"
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
echo "Teleport coords (spawn at Y 300, approach from above):"
for t in "${TILES[@]}"; do
  tx="${t%%:*}"; rest="${t#*:}"; tz="${rest%%:*}"; nm="${rest#*:}"
  echo "  /tp @s $((tx * 512 + 256)) 300 $((tz * 512 + 256))   # ($tx,$tz) $nm"
done

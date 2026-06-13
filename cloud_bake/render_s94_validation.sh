#!/usr/bin/env bash
# render_s94_validation.sh — S94 minimal validation render, FULL box lifecycle.
#
# Creates NBOX ccx63 boxes from the render-ready S93 snapshot, arms an
# auto-killer, round-robins the tile list, renders, health-checks on-box,
# collects with md5 verification, health-checks locally, installs to the
# verify world, deletes the boxes, and verifies 0 servers remain.
#
# BED-CACHE LAW: this script NEVER touches masks/_bed_cache_v17.pkl (the S93
# snapshot ships v17 + migrated v19). The health check asserts the log shows
# "bed cache MIGRATED/HIT" and never a rebuild.
#
# Usage:
#   NBOX=3 bash cloud_bake/render_s94_validation.sh
# Env knobs: NBOX(3) TILES(memory/verify_s94_tiles.txt) OUT_DIR(/d/s94_out)
#   DEST(verify world region/) THREADS(40) TTL_MIN(150 auto-kill) LOC(fsn1)
set -u
TOKEN_FILE="/c/Users/nicho/.hetzner_token"
IDS_FILE="/c/Users/nicho/.hetzner_ids"
SNAPSHOT_ID="${SNAPSHOT_ID:-396927540}"   # vandir-baked-s93
NBOX="${NBOX:-3}"
TILES="${TILES:-memory/verify_s94_tiles.txt}"
OUT_DIR="${OUT_DIR:-/d/s94_out}"
DEST="${DEST:-/d/modrinth_vandir/saves/Vandir50k_verify/region}"
BRANCH="s85-cherry-picks"
THREADS="${THREADS:-40}"; OMP="${OMP:-1}"
TTL_MIN="${TTL_MIN:-150}"
LOC="${LOC:-fsn1}"
API="https://api.hetzner.cloud/v1"
START=$(date +%s)
log(){ echo "[T+$(( ($(date +%s)-START)/60 ))m] $*"; }
PY_LOCAL="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"

[ -f "$TOKEN_FILE" ] || { echo "FATAL: no token at $TOKEN_FILE"; exit 2; }
TOKEN=$(cat "$TOKEN_FILE")
hz(){ curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }

mkdir -p "$OUT_DIR"

# ---- parse tile list (strip comments) ----------------------------------
mapfile -t TLINES < <(sed 's/#.*//' "$TILES" | tr -d '\r' | awk 'NF>=2 {print $1" "$2}')
NT=${#TLINES[@]}
[ "$NT" -ge 1 ] || { echo "FATAL: no tiles parsed from $TILES"; exit 2; }
log "S94 validation: $NT tiles on $NBOX boxes from snapshot $SNAPSHOT_ID"

# ---- 1. create servers ---------------------------------------------------
KEY_IDS=$(hz "$API/ssh_keys" | "$PY_LOCAL" -c "import json,sys; print(','.join(str(k['id']) for k in json.load(sys.stdin)['ssh_keys']))")
[ -n "$KEY_IDS" ] || { echo "FATAL: no ssh keys registered in Hetzner project"; exit 2; }
log "ssh key ids: $KEY_IDS"

IDS=(); IPS=()
for i in $(seq 1 "$NBOX"); do
  resp=$(hz -X POST "$API/servers" -d "{\"name\":\"vandir-s94-$i\",\"server_type\":\"ccx63\",\"image\":$SNAPSHOT_ID,\"location\":\"$LOC\",\"ssh_keys\":[$KEY_IDS],\"start_after_create\":true}")
  id=$(echo "$resp" | "$PY_LOCAL" -c "import json,sys; d=json.load(sys.stdin); print(d.get('server',{}).get('id',''))")
  ip=$(echo "$resp" | "$PY_LOCAL" -c "import json,sys; d=json.load(sys.stdin); print(d.get('server',{}).get('public_net',{}).get('ipv4',{}).get('ip',''))")
  if [ -z "$id" ] || [ -z "$ip" ]; then
    echo "FATAL: server create failed: $resp"
    # clean up any servers already created this run
    for cid in "${IDS[@]:-}"; do [ -n "$cid" ] && hz -X DELETE "$API/servers/$cid" >/dev/null; done
    exit 2
  fi
  IDS+=("$id"); IPS+=("$ip")
  log "created vandir-s94-$i id=$id ip=$ip"
done
printf "%s\n" "${IDS[@]}" > "$IDS_FILE"

# ---- 2. arm auto-killer BEFORE dispatch ----------------------------------
KILLER_LOG="$OUT_DIR/autokiller.log"
nohup bash -c "sleep $((TTL_MIN*60)); for id in ${IDS[*]}; do curl -s -X DELETE -H 'Authorization: Bearer $TOKEN' '$API/servers/'\$id >> '$KILLER_LOG' 2>&1; echo \"killed \$id at \$(date)\" >> '$KILLER_LOG'; done" >/dev/null 2>&1 &
KILLER_PID=$!
log "auto-killer armed: PID $KILLER_PID, TTL ${TTL_MIN}m, ids: ${IDS[*]}"

# ---- 3. wait for ssh -----------------------------------------------------
for ip in "${IPS[@]}"; do
  ssh-keygen -R "$ip" >/dev/null 2>&1
  for n in $(seq 1 40); do
    ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null
    if ssh -o ConnectTimeout=5 -o BatchMode=yes root@"$ip" true 2>/dev/null; then
      log "$ip ssh up"; break
    fi
    [ "$n" = 40 ] && { echo "FATAL: $ip never came up"; exit 2; }
    sleep 10
  done
done

# ---- 4. dispatch ----------------------------------------------------------
declare -a BOX_TILES; for b in $(seq 0 $((NBOX-1))); do BOX_TILES[$b]=""; done
i=0
for line in "${TLINES[@]}"; do
  b=$(( i % NBOX )); BOX_TILES[$b]="${BOX_TILES[$b]}|$line"; i=$((i+1))
done

dispatch(){
  local b="$1" ip="${IPS[$b]}" tiles="${BOX_TILES[$b]}"
  local lf="$OUT_DIR/render_s94_box${b}.log"; > "$lf"
  ssh root@"$ip" "cd /root/minecraft-worldgen && git fetch origin && \
    (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && \
    git reset --hard origin/$BRANCH && git log --oneline -1" 2>&1 | tee -a "$lf"
  # write the box's own tile list (for the on-box health check)
  echo "$tiles" | tr '|' '\n' | awk 'NF' | ssh root@"$ip" "cat > /root/rv_tiles.txt"
  local rowcmds=""
  IFS='|' read -ra TS <<< "$tiles"
  for t in "${TS[@]}"; do
    [ -z "${t// }" ] && continue
    local tx=${t%% *} tz=${t##* }; local tx1=$((tx+1)) tz1=$((tz+1))
    rowcmds+="python run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 $tx --tile-x1 $tx1 --tile-z0 $tz --tile-z1 $tz1 --threads $THREADS >> /root/rv_${tx}_${tz}.log 2>&1; "
  done
  # NOTE: no cache clear (bed-cache law), no mask upload (snapshot current),
  # no derived-mask build (snapshot ships S89 masks = render-ready).
  local cmd="cd /root/minecraft-worldgen && rm -f /root/rv_done /root/rv_health.txt /root/rv_*.log && rm -rf output && tmux kill-session -t rv 2>/dev/null; "
  cmd+="tmux new -d -s rv 'source /root/venv/bin/activate; export PYTHONUNBUFFERED=1 OMP_NUM_THREADS=$OMP OPENBLAS_NUM_THREADS=$OMP MKL_NUM_THREADS=$OMP; "
  cmd+="$rowcmds "
  cmd+="python tools/verify_render_health.py --out-dir output --tiles /root/rv_tiles.txt --logs \"/root/rv_*.log\" > /root/rv_health.txt 2>&1; "
  cmd+="(cd output && md5sum *.mca > /root/rv_md5.txt 2>/dev/null); touch /root/rv_done'"
  ssh root@"$ip" "$cmd" 2>&1 | tee -a "$lf"
  log "box$b ($ip) dispatched $(echo "$tiles" | tr '|' '\n' | grep -c ' ') tiles"
}
for b in $(seq 0 $((NBOX-1))); do dispatch "$b" & done
wait
log "all dispatched"

# ---- 5. monitor -----------------------------------------------------------
declare -A DONE; for b in $(seq 0 $((NBOX-1))); do DONE[$b]=0; done
while true; do
  all=1; st=""
  for b in $(seq 0 $((NBOX-1))); do
    ip="${IPS[$b]}"
    if [ "${DONE[$b]}" = "1" ]; then st="$st b$b=DONE"; continue; fi
    d=$(ssh -o ConnectTimeout=8 root@"$ip" "test -f /root/rv_done && echo Y || echo N" 2>/dev/null)
    n=$(ssh -o ConnectTimeout=8 root@"$ip" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
    if [ "$d" = "Y" ]; then DONE[$b]=1; st="$st b$b=DONE(${n:-?})"; else all=0; st="$st b$b=${n:-0}"; fi
  done
  log "$st"; [ "$all" = "1" ] && break; sleep 30
done

# ---- 6. on-box health verdicts -------------------------------------------
HEALTH_FAIL=0
for b in $(seq 0 $((NBOX-1))); do
  ip="${IPS[$b]}"
  ssh root@"$ip" "cat /root/rv_health.txt" > "$OUT_DIR/health_box${b}.txt" 2>/dev/null
  if grep -q "RESULT: PASS" "$OUT_DIR/health_box${b}.txt"; then
    log "box$b health: PASS"
  else
    log "box$b health: FAIL — see $OUT_DIR/health_box${b}.txt"
    HEALTH_FAIL=1
  fi
  # bed-cache law assertion, belt and braces (health check also does this)
  if ssh root@"$ip" "grep -lE '_BedCacheRefusal|falling back to rebuild' /root/rv_*.log" 2>/dev/null | grep -q .; then
    log "box$b BED CACHE VIOLATION — rivers untrustworthy"
    HEALTH_FAIL=1
  fi
done

# ---- 7. collect + md5 verify ----------------------------------------------
log "collecting to $OUT_DIR"
for b in $(seq 0 $((NBOX-1))); do
  ip="${IPS[$b]}"
  ssh -o ConnectTimeout=20 root@"$ip" "tar cf - -C /root/minecraft-worldgen/output ." 2>/dev/null | tar xf - -C "$OUT_DIR" &
  ssh root@"$ip" "cat /root/rv_md5.txt" > "$OUT_DIR/md5_box${b}.txt" 2>/dev/null
done
wait
cat "$OUT_DIR"/md5_box*.txt > "$OUT_DIR/md5_all.txt"
MD5_FAIL=0
( cd "$OUT_DIR" && md5sum -c md5_all.txt > md5_check.txt 2>&1 ) || MD5_FAIL=1
log "md5 verify: $(grep -c ': OK' "$OUT_DIR/md5_check.txt" 2>/dev/null || echo 0) OK, fail=$MD5_FAIL"

# ---- 8. local health check -------------------------------------------------
"$PY_LOCAL" tools/verify_render_health.py --out-dir "$OUT_DIR" --tiles "$TILES" > "$OUT_DIR/health_local.txt" 2>&1
LOCAL_RC=$?
tail -5 "$OUT_DIR/health_local.txt"

# ---- 9. delete boxes + verify 0 remaining -----------------------------------
log "deleting ${#IDS[@]} boxes"
kill "$KILLER_PID" 2>/dev/null
for id in "${IDS[@]}"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE -H "Authorization: Bearer $TOKEN" "$API/servers/$id")
  log "delete $id -> HTTP $code"
done
sleep 5
REMAIN=$(hz "$API/servers" | "$PY_LOCAL" -c "import json,sys; print(len(json.load(sys.stdin).get('servers',[])))")
log "servers remaining: $REMAIN"
[ "$REMAIN" = "0" ] && rm -f "$IDS_FILE"

# ---- 10. install (only on full PASS) ----------------------------------------
if [ "$LOCAL_RC" = "0" ] && [ "$MD5_FAIL" = "0" ] && [ "$HEALTH_FAIL" = "0" ]; then
  if [ -d "$DEST" ]; then
    cp -f "$OUT_DIR"/r.*.mca "$DEST/"
    # md5-verify the install copy
    ( cd "$DEST" && md5sum $(awk '{print $2}' "$OUT_DIR/md5_all.txt" | tr -d '*') ) > "$OUT_DIR/md5_installed.txt" 2>/dev/null
    if diff <(sort "$OUT_DIR/md5_all.txt") <(sort "$OUT_DIR/md5_installed.txt" | sed "s|$DEST/||") >/dev/null 2>&1; then
      log "INSTALLED + md5-verified -> $DEST"
    else
      log "installed but md5 mismatch vs collected — RE-COPY NEEDED"
    fi
  else
    log "WARN: DEST $DEST missing — skipped install"
  fi
  log "S94 RENDER: ALL GATES PASS"
else
  log "S94 RENDER: GATES FAILED (local_rc=$LOCAL_RC md5_fail=$MD5_FAIL box_health_fail=$HEALTH_FAIL) — NOT installed. Outputs kept in $OUT_DIR"
fi
log "DONE in $(( ($(date +%s)-START)/60 ))m. Remaining servers: $REMAIN (must be 0)"

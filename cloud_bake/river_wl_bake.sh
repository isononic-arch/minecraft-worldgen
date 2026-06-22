#!/usr/bin/env bash
# river_wl_bake.sh — S94 global river water-level bake + seam verify, 1 box.
#
# Single box, SEQUENTIAL dispatch (no backgrounded dispatch&wait -> avoids the
# render_s94_seam.sh hang where the parent blocked in `wait` on a dangling ssh
# channel and never collected). Drives the poll+collect+delete in the foreground.
#
#   1. provision 1 ccx63, arm auto-killer
#   2. git reset --hard origin/branch
#   3. tmux: rebuild_river_wl.py --scale 1  (full 50k, ~192GB)  ->  hydro_river_wl.tif
#            then render the 4 river-crossing verify tiles (reads the new mask)
#            then health-check + md5 + touch /root/done
#   4. foreground poll until /root/done
#   5. scp back hydro_river_wl.tif + the 4 MCAs + logs
#   6. delete box, verify 0 remaining
#
# Usage: bash cloud_bake/river_wl_bake.sh
set -u
TOKEN_FILE="/c/Users/nicho/.hetzner_token"
SNAPSHOT_ID="${SNAPSHOT_ID:-396927540}"
BOX_NAME="${BOX_NAME:-vandir-riverwl}"   # MUST be unique per concurrent box (Hetzner uniqueness_error)
OUT_DIR="${OUT_DIR:-/d/river_wl_out}"
BRANCH="s85-cherry-picks"
THREADS="${THREADS:-8}"; OMP="${OMP:-4}"
COVER="${COVER:-256}"           # river-wl level coverage band (blocks; 256 = full coverage, validated)
BANK_COVER="${BANK_COVER:-6}"   # S94c: blocks out from centerline to measure the BANK (containment). small=contained, large(28)=over-levels
TTL_MIN="${TTL_MIN:-120}"       # EDT-free rebuild is fast; headroom for safety
LOC="${LOC:-fsn1}"
TILES="${TILES:-52,53;52,54;12,80;13,80}"
API="https://api.hetzner.cloud/v1"
START=$(date +%s)
log(){ echo "[T+$(( ($(date +%s)-START)/60 ))m] $*"; }
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
[ -f "$TOKEN_FILE" ] || { echo "FATAL no token"; exit 2; }
TOKEN=$(cat "$TOKEN_FILE")
hz(){ curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }
mkdir -p "$OUT_DIR"

KEY_IDS=$(hz "$API/ssh_keys" | "$PY" -c "import json,sys;print(','.join(str(k['id']) for k in json.load(sys.stdin)['ssh_keys']))")
[ -n "$KEY_IDS" ] || { echo "FATAL no ssh keys"; exit 2; }

resp=$(hz -X POST "$API/servers" -d "{\"name\":\"$BOX_NAME\",\"server_type\":\"ccx63\",\"image\":$SNAPSHOT_ID,\"location\":\"$LOC\",\"ssh_keys\":[$KEY_IDS],\"start_after_create\":true}")
ID=$(echo "$resp" | "$PY" -c "import json,sys;print(json.load(sys.stdin).get('server',{}).get('id',''))")
IP=$(echo "$resp" | "$PY" -c "import json,sys;print(json.load(sys.stdin).get('server',{}).get('public_net',{}).get('ipv4',{}).get('ip',''))")
[ -n "$ID" ] && [ -n "$IP" ] || { echo "FATAL create failed: $resp"; exit 2; }
log "created vandir-riverwl id=$ID ip=$IP"

# auto-killer BEFORE any work
KILLER_LOG="$OUT_DIR/autokiller.log"
nohup bash -c "sleep $((TTL_MIN*60)); curl -s -X DELETE -H 'Authorization: Bearer $TOKEN' '$API/servers/$ID' >> '$KILLER_LOG' 2>&1; echo killed-$ID >> '$KILLER_LOG'" >/dev/null 2>&1 &
KILLER_PID=$!
log "auto-killer armed PID $KILLER_PID TTL ${TTL_MIN}m id=$ID"

# wait ssh
ssh-keygen -R "$IP" >/dev/null 2>&1
for n in $(seq 1 40); do
  ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null
  ssh -o ConnectTimeout=5 -o BatchMode=yes root@"$IP" true 2>/dev/null && { log "ssh up"; break; }
  [ "$n" = 40 ] && { echo "FATAL ssh never up"; curl -s -X DELETE -H "Authorization: Bearer $TOKEN" "$API/servers/$ID">/dev/null; exit 2; }
  sleep 10
done

# update code
ssh root@"$IP" "cd /root/minecraft-worldgen && git fetch origin && (git checkout $BRANCH 2>/dev/null || git checkout -t origin/$BRANCH) && git reset --hard origin/$BRANCH && git log --oneline -1" 2>&1 | tee "$OUT_DIR/box.log"

# tile-list file for health check
echo "$TILES" | tr ';' '\n' | tr ',' ' ' | ssh root@"$IP" "cat > /root/rv_tiles.txt"

# dispatch: rebuild (50k) -> render 4 tiles -> health+md5 -> done. tmux detaches,
# output redirected, so this ssh RETURNS cleanly (no dangling channel).
JOB="source /root/venv/bin/activate; export PYTHONUNBUFFERED=1 OMP_NUM_THREADS=$OMP OPENBLAS_NUM_THREADS=$OMP MKL_NUM_THREADS=$OMP; cd /root/minecraft-worldgen; rm -f /root/done /root/job.log; echo REBUILD_START > /root/job.log; python rebuild_ocean_dist.py >> /root/job.log 2>&1; if [ -n \"\${RIVER_WL_OVERRIDE:-}\" ]; then python rebuild_river_wl.py --scale 1 --cover $COVER --bank-cover $BANK_COVER >> /root/job.log 2>&1; fi; echo RENDER_START >> /root/job.log; python run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-list \"$TILES\" --threads $THREADS >> /root/job.log 2>&1; python tools/verify_render_health.py --out-dir output --tiles /root/rv_tiles.txt > /root/health.txt 2>&1; (cd output && md5sum *.mca > /root/md5.txt 2>/dev/null); cp masks/hydro_river_wl.tif /root/hydro_river_wl.tif; touch /root/done"
ssh root@"$IP" "tmux kill-session -t job 2>/dev/null; tmux new -d -s job '$JOB'" < /dev/null
log "dispatched (rebuild + 4-tile render) in tmux"

# foreground poll, with a HARD timeout so it can never spin for hours if the box
# stalls/dies (the prior failure mode: box auto-killed mid-rebuild, poll spun 4h).
POLL_DEADLINE=$(( START + (TTL_MIN + 15) * 60 ))
while true; do
  if [ "$(date +%s)" -gt "$POLL_DEADLINE" ]; then
    log "POLL TIMEOUT (> ${TTL_MIN}+15m) — box stalled/dead; aborting poll, will delete"
    break
  fi
  d=$(ssh -o ConnectTimeout=8 root@"$IP" "test -f /root/done && echo Y || echo N" 2>/dev/null)
  tail=$(ssh -o ConnectTimeout=8 root@"$IP" "tail -1 /root/job.log 2>/dev/null" 2>/dev/null)
  n=$(ssh -o ConnectTimeout=8 root@"$IP" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
  log "done=$d mcas=${n:-0} | $tail"
  [ "$d" = "Y" ] && break
  sleep 45
done

# collect
log "collecting"
scp -q root@"$IP":/root/hydro_river_wl.tif "$OUT_DIR/" 2>/dev/null
scp -q root@"$IP":/root/minecraft-worldgen/output/r.*.mca "$OUT_DIR/" 2>/dev/null
ssh root@"$IP" "cat /root/health.txt" > "$OUT_DIR/health.txt" 2>/dev/null
ssh root@"$IP" "cat /root/md5.txt" > "$OUT_DIR/md5.txt" 2>/dev/null
ssh root@"$IP" "grep -E 'river_wl|RENDER_START|Done:|error|Traceback' /root/job.log" > "$OUT_DIR/job_tail.txt" 2>/dev/null
log "collected: $(ls $OUT_DIR/r.*.mca 2>/dev/null | wc -l) MCAs, mask $(ls -la $OUT_DIR/hydro_river_wl.tif 2>/dev/null | awk '{print $5}')"
grep RESULT "$OUT_DIR/health.txt" 2>/dev/null

# delete + verify 0
kill "$KILLER_PID" 2>/dev/null
code=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE -H "Authorization: Bearer $TOKEN" "$API/servers/$ID")
log "delete $ID -> HTTP $code"
sleep 5
REMAIN=$(hz "$API/servers" | "$PY" -c "import json,sys;print(len(json.load(sys.stdin).get('servers',[])))")
log "servers remaining: $REMAIN (must be 0)"
log "DONE in $(( ($(date +%s)-START)/60 ))m"

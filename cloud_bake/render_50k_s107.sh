#!/usr/bin/env bash
# render_50k_s107.sh — S107 mainland render, BOX-DIRECT-TO-BLOOMHOST.
#
# = render_50k_final.sh (validated S93 input handling: snapshot override left
# untouched; heal in place; force bed-cache regen; single-threaded PRE-WARM;
# per-box scp of the promoted S100 override/lithology masks; --skip-list for the
# 206 island-owned regions) PLUS the S107 push flow:
#
#   render rows -> health+md5 -> touch /root/rendered
#     -> box_push.py --wait-go   (waits for VandirWorld_S107/.go, which the home
#        gate daemon writes only after metadata is preserved AND the first box
#        verified AND VandirWorld_S106 is deleted; pushes serialized by the
#        .push_lock SFTP mutex, 16 conns, size-verified, 3 re-push rounds)
#     -> touch /root/done        (the monitor's flag now means RENDERED+PUSHED)
#
# DISPATCH-ONLY BY DESIGN (one babysitter rule): creates + dispatches + writes
# live_boxes.txt, then EXITS. poll/collect/reap = cloud_bake/render_monitor.py
# against the runspec from make_mainland_runspec.py --s107. Gate = s107_gate.py.
#
# Refire a crashed box's rows:  ROWS_OVERRIDE="17 33" BOX_INDEX=9 bash cloud_bake/render_50k_s107.sh
# (creates ONE box named vandir-50k-9 rendering exactly those rows.)
set -u
TOKEN_FILE="/c/Users/nicho/.hetzner_token"
SNAPSHOT_ID="${SNAPSHOT_ID:-396927540}"; BRANCH="master"
NBOXES="${NBOXES:-8}"; GRID=97
THREADS="${THREADS:-40}"; OMP="${OMP:-1}"
# render+push realistically ~72m/box; 135m TTL = ~2x margin AND caps worst-case
# all-hang billing: 8 boxes x 135m x E1.6138/h = ~E29 mainland (islands similar;
# sequential => total worst-case < E60). ccx63 rounds UP to the hour, so this is
# the real cost lever. Monitor reaps far earlier (stall 30m / ttl 120m); box
# self-kill + box_guard sweeper are the outer backstops.
TTL_MIN="${TTL_MIN:-135}"
LOC="${LOC:-fsn1}"; OUT_ROOT="${OUT_ROOT:-/d/render_s107}"
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
CREDS_LOCAL="/c/Users/nicho/.bloom_creds.json"
SKIP_LIST="cloud_bake/mainland_skip_regions_s101.txt"
START=$(date +%s); log(){ echo "[T+$(( ($(date +%s)-START)/60 ))m] $*"; }
TOKEN=$(cat "$TOKEN_FILE"); hz(){ curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }
mkdir -p "$OUT_ROOT"
[ -f "$CREDS_LOCAL" ] || { echo "FATAL: $CREDS_LOCAL missing"; exit 2; }
KEY_IDS=$(hz "$API/ssh_keys" | "$PY" -c "import json,sys;print(','.join(str(k['id']) for k in json.load(sys.stdin)['ssh_keys']))")

# guard armed BEFORE any box exists (sweeper deletes ttl_min-labelled strays)
"$PY" cloud_bake/box_guard.py arm
"$PY" cloud_bake/box_guard.py status

# round-robin z-rows -> per-box row list (box b gets rows b, b+NB, ...) — or a
# single refire box with ROWS_OVERRIDE.
declare -a BOX_ROWS BOX_IDX
if [ -n "${ROWS_OVERRIDE:-}" ]; then
  NBOXES=1; BOX_ROWS[0]="$ROWS_OVERRIDE"; BOX_IDX[0]="${BOX_INDEX:-9}"
  log "REFIRE MODE: 1 box (vandir-50k-${BOX_IDX[0]}) rows: $ROWS_OVERRIDE"
else
  for ((b=0;b<NBOXES;b++)); do BOX_ROWS[$b]=""; BOX_IDX[$b]=$b; done
  for ((z=0;z<GRID;z++)); do b=$((z % NBOXES)); BOX_ROWS[$b]="${BOX_ROWS[$b]} $z"; done
fi

LOCS=($LOC nbg1 hel1 hil fsn1)
declare -A IP ID
for ((i=0;i<NBOXES;i++)); do
  bi="${BOX_IDX[$i]}"
  ID[$i]=""; IP[$i]=""
  for loc in "${LOCS[$((i % 4))]}" "${LOCS[@]:0:4}"; do
    resp=$(hz -X POST "$API/servers" -d "{\"name\":\"vandir-50k-$bi\",\"server_type\":\"ccx63\",\"image\":$SNAPSHOT_ID,\"location\":\"$loc\",\"ssh_keys\":[$KEY_IDS],\"start_after_create\":true,\"labels\":{\"ttl_min\":\"$TTL_MIN\"}}")
    ID[$i]=$(echo "$resp"|"$PY" -c "import json,sys;print(json.load(sys.stdin).get('server',{}).get('id',''))")
    IP[$i]=$(echo "$resp"|"$PY" -c "import json,sys;print(json.load(sys.stdin).get('server',{}).get('public_net',{}).get('ipv4',{}).get('ip',''))")
    [ -n "${ID[$i]}" ] && { log "created vandir-50k-$bi id=${ID[$i]} @$loc (ttl ${TTL_MIN}m)"; break; }
    log "  vandir-50k-$bi@$loc create failed; next loc"
  done
  [ -n "${ID[$i]}" ] && [ -n "${IP[$i]}" ] || { echo "FATAL create box$bi after all locations: $resp"; exit 2; }
  echo "$(date +%s) CREATE vandir-50k-$bi ${ID[$i]} ccx63" >> "$OUT_ROOT/boxes_log.txt"
  nohup bash -c "sleep $((TTL_MIN*60)); curl -s -X DELETE -H 'Authorization: Bearer $TOKEN' '$API/servers/${ID[$i]}' >> '$OUT_ROOT/autokiller.log' 2>&1; echo killed-${ID[$i]} >> '$OUT_ROOT/autokiller.log'" >/dev/null 2>&1 &
  sleep 3
done

# dispatch: git reset -> masks scp -> creds+paramiko -> heal -> bed regen ->
# pre-warm -> render rows -> health/md5 -> rendered flag -> gated push -> done
for ((i=0;i<NBOXES;i++)); do
  bi="${BOX_IDX[$i]}"
  ip="${IP[$i]}"; rows="${BOX_ROWS[$i]}"; R0=$(echo $rows | awk '{print $1}')
  ssh-keygen -R "$ip" >/dev/null 2>&1
  for n in $(seq 1 40); do ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null
    ssh -o ConnectTimeout=5 -o BatchMode=yes root@"$ip" true 2>/dev/null && { log "box$bi ssh up"; break; }
    [ "$n" = 40 ] && { echo "FATAL box$bi ssh"; exit 2; }; sleep 10; done
  ssh root@"$ip" "cd /root/minecraft-worldgen && git fetch origin && git reset --hard origin/$BRANCH && git log --oneline -1" 2>&1 | tee -a "$OUT_ROOT/box$bi.boxlog"
  log "box$bi: uploading override + lithology masks (snapshot copies are stale) + bloom creds"
  scp -q masks/override.tif         root@"$ip":/root/minecraft-worldgen/masks/override.tif
  scp -q masks/lithology.tif        root@"$ip":/root/minecraft-worldgen/masks/lithology.tif
  scp -q masks/lithology_region.png root@"$ip":/root/minecraft-worldgen/masks/lithology_region.png
  scp -q "$CREDS_LOCAL"             root@"$ip":/root/bloom_creds.json
  ssh root@"$ip" "/root/venv/bin/pip install -q paramiko" 2>&1 | tail -1
  # expected region count for THIS box's rows (rows minus skip-listed regions)
  MINC=$(ROWS="$rows" "$PY" -c "
import os
skip={tuple(map(int,l.split())) for l in open(r'$SKIP_LIST') if l.strip()}
rows=[int(z) for z in os.environ['ROWS'].split()]
print(sum(1 for z in rows for x in range(97) if (x,z) not in skip))")
  log "box$bi: expects $MINC regions from rows:$rows"
  RP="python run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/"
  ROWCMDS=""
  # PRE-WARM deliberately has NO --skip-list (bed-cache must build; see final.sh)
  for z in $rows; do z1=$((z+1))
    ROWCMDS+="echo ROW_${z}_START >> /root/job.log; $RP --tile-x0 0 --tile-x1 $GRID --tile-z0 $z --tile-z1 $z1 --skip-list $SKIP_LIST --threads $THREADS >> /root/job.log 2>&1; "
  done
  JOB="source /root/venv/bin/activate; export PYTHONUNBUFFERED=1 OMP_NUM_THREADS=$OMP OPENBLAS_NUM_THREADS=$OMP MKL_NUM_THREADS=$OMP; cd /root/minecraft-worldgen; rm -f /root/done /root/rendered /root/job.log; "
  JOB+="echo HEAL_START >> /root/job.log; python tools/heal_height_seams.py --inplace >> /root/job.log 2>&1; "
  JOB+="echo REGEN_START >> /root/job.log; rm -f masks/_bed_cache_v17.pkl masks/_bed_cache_v19.pkl; "
  JOB+="echo PREWARM_START >> /root/job.log; $RP --tile-x0 0 --tile-x1 1 --tile-z0 $R0 --tile-z1 $((R0+1)) --threads 1 >> /root/job.log 2>&1; "
  JOB+="echo RENDER_START >> /root/job.log; $ROWCMDS"
  JOB+="python tools/verify_render_health.py --out-dir output > /root/health.txt 2>&1; (cd output && md5sum *.mca > /root/md5.txt 2>/dev/null); touch /root/rendered; "
  JOB+="echo PUSH_START >> /root/job.log; python cloud_bake/box_push.py --creds /root/bloom_creds.json --src output --min-count $MINC --skip-list $SKIP_LIST --wait-go --gate-timeout-min 120 >> /root/job.log 2>&1 && touch /root/done"
  ssh root@"$ip" "tmux kill-session -t r50 2>/dev/null; tmux new -d -s r50 '$JOB'" < /dev/null
  log "box$bi dispatched: $(echo $rows|wc -w) rows (prewarm tile 0,$R0; push-gated)"
  # billing-stop backstop even if this PC dies: on-box self-DELETE via metadata API
  "$PY" cloud_bake/box_guard.py selfdestruct "$ip" --minutes $TTL_MIN --delete-via-api
done

for ((b=0;b<NBOXES;b++)); do echo "${ID[$b]} ${IP[$b]}"; done > "$OUT_ROOT/live_boxes.txt"
log "=== DISPATCH DONE — boxes rendering. Next steps (see RUNBOOK_S107.md): ==="
log "  \"$PY\" cloud_bake/make_mainland_runspec.py --nboxes $NBOXES --s107"
log "  \"$PY\" cloud_bake/s107_gate.py cloud_bake/runspec_mainland.json        (detached)"
log "  \"$PY\" cloud_bake/render_monitor.py cloud_bake/runspec_mainland.json   (detached)"

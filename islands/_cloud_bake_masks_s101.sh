#!/usr/bin/env bash
# _cloud_bake_masks_s101.sh — S101 MASK-BAKE-ONLY cloud run.
# ONE ccx63 (48 vCPU/192GB) bakes all 15 islands CONCURRENTLY (bakes are
# single-threaded numpy → wall = slowest island, ~10 min; more boxes would NOT
# be faster, just costlier). Pushes code tarball + supplemental bake inputs the
# code tarball never carried (erase_masks, island_geo_data) + the 8 non-prerot
# DEMs; prerot DEMs ride in the tarball and are copied to /root/dems on-box.
# Collects islands/masks_islands as /tmp/masks.tgz -> islands/_collect_bake_s101/,
# then DELETES the box + safety-sweeps by name prefix + verifies 0 servers.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
TGZ="/tmp/rv_code_s101.tgz"
SUPP="/tmp/rv_supp_s101.tgz"
COL="$ROOT/islands/_collect_bake_s101"; mkdir -p "$COL"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
KEYID=112518810
IMG=396927540
BOXNAME="s101-bake"
START=$(date +%s)
log(){ echo "[$(date +%H:%M:%S)] $*"; }
api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }

source "$ROOT/islands/box_watchdog.sh"

# ---- 0. bundles ----
TGZ="$TGZ" bash "$ROOT/islands/make_tarball.sh" || { log "!! make_tarball failed"; exit 1; }
tar czf "$SUPP" islands/erase_masks islands/island_geo_data islands/_box_bake_all_s101.sh \
  || { log "!! supp tarball failed"; exit 1; }
log "bundles: $(du -h "$TGZ" | cut -f1) code + $(du -h "$SUPP" | cut -f1) supp"

# ---- 1. create (loc fallback) ----
SID=""; IP=""
for loc in fsn1 nbg1 hel1 hil; do
  body=$(printf '{"name":"%s","server_type":"ccx63","image":%d,"location":"%s","ssh_keys":[%d],"start_after_create":true}' \
                "$BOXNAME" "$IMG" "$loc" "$KEYID")
  resp=$(api -X POST "$API/servers" -d "$body")
  sid=$(echo "$resp" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('server',{}).get('id',''))" 2>/dev/null)
  if [ -n "$sid" ]; then SID=$sid; log "created $BOXNAME @$loc id=$sid"; break; fi
  log "  create @$loc failed; next loc"
done
[ -z "$SID" ] && { log "!! create FAILED all locations"; exit 1; }

# ---- 2. wait running + IP ----
for _ in $(seq 1 60); do
  r=$(api "$API/servers/$SID")
  st=$(echo "$r" | "$PY" -c "import sys,json;s=json.load(sys.stdin)['server'];print(s['status'],s['public_net']['ipv4']['ip'])" 2>/dev/null)
  set -- $st; status="${1:-}"; ip="${2:-}"
  if [ "$status" = "running" ] && [ -n "$ip" ]; then IP=$ip; log "running ip=$ip"; break; fi
  sleep 6
done
[ -z "$IP" ] && { log "!! never reached running; deleting"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }

# ---- 3. deploy ----
up=0
for _ in $(seq 1 40); do ssh $SSHO root@"$IP" true 2>/dev/null && { up=1; break; }; sleep 6; done
[ "$up" = 0 ] && { log "!! ssh never up; deleting"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }
log "ssh up; pushing code + supp + 8 non-prerot DEMs"
ssh $SSHO root@"$IP" "mkdir -p /root/dems" 2>/dev/null
scp $SSHO -q "$TGZ"  root@"$IP":/tmp/rv_code.tgz || { log "!! scp code failed"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }
scp $SSHO -q "$SUPP" root@"$IP":/tmp/rv_supp.tgz || { log "!! scp supp failed"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }
for k in 13_130 11_060 23_887 10_941 21_395 -17_622 49_722 -50_393; do
  demf=$("$PY" "$ROOT/islands/_dem_gitpath.py" "$k")
  [ -f "$demf" ] || { log "!! DEM missing for $k ($demf)"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }
  scp $SSHO -q "$demf" root@"$IP":/root/dems/"$(basename "$demf")" \
    && log "  DEM $k pushed" || { log "!! scp DEM $k failed"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }
done

ssh $SSHO root@"$IP" "bash -s" <<'EOF' 2>/dev/null
set -e
cd /root/minecraft-worldgen
tar xzf /tmp/rv_code.tgz
tar xzf /tmp/rv_supp.tgz
cp islands/prerot_dems/*.png /root/dems/ 2>/dev/null || true
/root/venv/bin/python - <<'PY'
import json,os
lay=json.load(open("islands/layout.json"))
for isl in lay["islands"]:
    bn=os.path.basename(isl["dem_path"].replace("\\","/"))
    isl["dem_path"]="/root/dems/"+bn
json.dump(lay,open("islands/layout.json","w"),indent=2)
print("layout rewritten")
PY
rm -f /root/all_done /root/bake_results
tmux kill-server 2>/dev/null || true
tmux new-session -d -s bake "bash /root/minecraft-worldgen/islands/_box_bake_all_s101.sh > /root/run.log 2>&1"
echo launched
EOF
log "LAUNCHED 15 concurrent bakes"
box_arm_selfdestruct "$IP" 75

# ---- 4. poll (progress via bake_results) + collect + delete ----
SEEN=0
DEADLINE=$((START + 2700))   # 45 min hard cap
while :; do
  now=$(date +%s)
  [ "$now" -gt "$DEADLINE" ] && { log "!! 45m cap hit"; break; }
  probe=$(ssh $SSHO root@"$IP" 'test -f /root/all_done && echo __DONE__; cat /root/bake_results 2>/dev/null' 2>/dev/null)
  n=$(printf '%s\n' "$probe" | grep -c '^\(OK\|FAIL\) ' 2>/dev/null)
  if [ "${n:-0}" -gt "$SEEN" ]; then
    printf '%s\n' "$probe" | grep '^\(OK\|FAIL\) ' | tail -n $((n - SEEN)) | while read -r ln; do log "  $ln"; done
    SEEN=$n
  fi
  case "$probe" in *__DONE__*) log "all_done seen"; break;; esac
  sleep 20
done

log "collecting masks.tgz"
if scp $SSHO -q root@"$IP":/tmp/masks.tgz "$COL/masks.tgz" 2>/dev/null && [ -s "$COL/masks.tgz" ]; then
  ssh $SSHO root@"$IP" "cat /root/all_done" 2>/dev/null | while read -r ln; do log "  box: $ln"; done
  log "collected $(du -h "$COL/masks.tgz" | cut -f1)"
else
  log "!! collect FAILED; saving logs"
  ssh $SSHO root@"$IP" "tail -n 40 /root/run.log; tail -n 20 /root/bakelogs/*.log" > "$COL/FAIL.log" 2>/dev/null
fi

api -X DELETE "$API/servers/$SID" >/dev/null 2>&1; log "DELETED box id=$SID"

# ---- 5. safety sweep + verify ----
rem=$(api "$API/servers" | "$PY" -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin).get('servers',[]) if s['name'].startswith('$BOXNAME')]" 2>/dev/null)
for id in $rem; do api -X DELETE "$API/servers/$id" >/dev/null 2>&1; log "safety-deleted $id"; done
log "verify: $(api "$API/servers" | "$PY" -c "import sys,json;print(len(json.load(sys.stdin).get('servers',[])),'servers remain')")"
log "DONE elapsed $(( ($(date +%s)-START)/60 ))m ; tarball: $COL/masks.tgz"

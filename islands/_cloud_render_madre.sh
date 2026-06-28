#!/usr/bin/env bash
# Madre-only 1-box re-render (keep_box fix). Mirrors _cloud_render_s95.sh lifecycle
# for a single ccx63 box rendering key -50_393. Collects islands/out -> _collect_madre.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"
cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
DL="/c/Users/nicho/Downloads"
TGZ="/tmp/rv_code.tgz"
COL="$ROOT/islands/_collect_madre"; mkdir -p "$COL"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
KEYID=112518810
IMG=396927540
START=$(date +%s)
log(){ echo "[$(date +%H:%M:%S)] $*"; }
api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }

NAME=isl-madre
KEY="-50_393"
WORK=44
ALLLOC=(fsn1 nbg1 hel1 hil)

# ---- create ----
SID=""
for loc in "${ALLLOC[@]}"; do
  body=$(printf '{"name":"%s","server_type":"ccx63","image":%d,"location":"%s","ssh_keys":[%d],"start_after_create":true}' "$NAME" "$IMG" "$loc" "$KEYID")
  resp=$(api -X POST "$API/servers" -d "$body")
  SID=$(echo "$resp" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('server',{}).get('id',''))" 2>/dev/null)
  if [ -n "$SID" ]; then log "create $NAME ccx63@$loc key=$KEY -> id=$SID"; break; fi
  log "  create @$loc failed; next"
done
[ -z "$SID" ] && { log "!! create FAILED all locs"; exit 1; }

# ---- wait running + IP ----
IP=""
for _ in $(seq 1 60); do
  r=$(api "$API/servers/$SID")
  st=$(echo "$r" | "$PY" -c "import sys,json;s=json.load(sys.stdin)['server'];print(s['status'],s['public_net']['ipv4']['ip'])" 2>/dev/null)
  set -- $st; status="${1:-}"; ip="${2:-}"
  if [ "$status" = "running" ] && [ -n "$ip" ]; then IP=$ip; log "$NAME running ip=$ip"; break; fi
  sleep 6
done
[ -z "$IP" ] && { log "!! never running; deleting"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }

# ---- wait sshd ----
up=0
for _ in $(seq 1 40); do ssh $SSHO root@"$IP" true 2>/dev/null && { up=1; break; }; sleep 6; done
[ "$up" = 0 ] && { log "!! ssh never up; deleting"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }

# ---- deploy + launch ----
log "ssh up; pushing code+DEM"
ssh $SSHO root@"$IP" "mkdir -p /root/dems" 2>/dev/null
scp $SSHO -q "$TGZ" root@"$IP":/tmp/rv_code.tgz 2>/dev/null || { log "!! scp code failed"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }
f=$(ls "$DL/${KEY}_"*_16bit.png 2>/dev/null | head -1)
scp $SSHO -q "$f" root@"$IP":/root/dems/ 2>/dev/null || { log "!! scp DEM failed"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }
ssh $SSHO root@"$IP" "bash -s" <<EOF 2>/dev/null
set -e
cd /root/minecraft-worldgen
tar xzf /tmp/rv_code.tgz
/root/venv/bin/python - <<'PY'
import json,os
lay=json.load(open("islands/layout.json"))
for isl in lay["islands"]:
    bn=os.path.basename(isl["dem_path"].replace("\\\\","/"))
    isl["dem_path"]="/root/dems/"+bn
json.dump(lay,open("islands/layout.json","w"),indent=2)
print("layout rewritten")
PY
rm -f /root/all_done
tmux kill-server 2>/dev/null || true
tmux new-session -d -s isl "cd /root/minecraft-worldgen && /root/venv/bin/python islands/_box_all_run.py $WORK $KEY > /root/run.log 2>&1"
echo launched
EOF
log "$NAME LAUNCHED (workers=$WORK key=$KEY)"

# ---- poll-collect-delete (3h cap) ----
while :; do
  if ssh $SSHO root@"$IP" "test -f /root/all_done" 2>/dev/null; then
    log "$NAME all_done; collecting"
    ssh $SSHO root@"$IP" "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz islands/out 2>/dev/null" 2>/dev/null
    if scp $SSHO -q root@"$IP":/tmp/out.tgz "$COL/madre.tgz" 2>/dev/null && [ -s "$COL/madre.tgz" ]; then
      ssh $SSHO root@"$IP" "cat /root/all_done" 2>/dev/null | while read -r ln; do log "    $ln"; done
      log "collected -> $COL/madre.tgz ($(du -h "$COL/madre.tgz"|cut -f1))"
      api -X DELETE "$API/servers/$SID" >/dev/null 2>&1; log "DELETED $NAME (id=$SID)"
      break
    else
      log "!! collect empty/failed; retry"
    fi
  fi
  if [ $(( $(date +%s) - START )) -gt 10800 ]; then
    log "!! 3h cap; deleting"; api -X DELETE "$API/servers/$SID" >/dev/null 2>&1; break
  fi
  sleep 45
done

# ---- safety sweep ----
rem=$(api "$API/servers" | "$PY" -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin).get('servers',[]) if s['name']=='isl-madre']" 2>/dev/null)
for id in $rem; do api -X DELETE "$API/servers/$id" >/dev/null 2>&1; log "safety-deleted $id"; done
log "=== MADRE DONE; elapsed $(( ($(date +%s)-START)/60 ))m ==="

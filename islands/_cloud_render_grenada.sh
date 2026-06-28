#!/usr/bin/env bash
# Grenada-only 1-box render (pre-rotated DEM, rot_deg=0). The pre-rotated DEM ships
# INSIDE the tarball (islands/prerot_dems/) so we cp it to /root/dems on-box rather
# than scp from Downloads. Collects islands/out -> _collect_grenada. Auto-deletes box.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"
cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
TGZ="/tmp/rv_code.tgz"
COL="$ROOT/islands/_collect_grenada"; mkdir -p "$COL"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
KEYID=112518810
IMG=396927540
START=$(date +%s)
log(){ echo "[$(date +%H:%M:%S)] $*"; }
api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }

NAME=isl-grenada
KEY="12_445"
DEMFILE="12_445_prerot_16bit.png"
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

# ---- deploy + launch (DEM is inside the tarball) ----
log "ssh up; pushing code(+prerot DEM)"
scp $SSHO -q "$TGZ" root@"$IP":/tmp/rv_code.tgz 2>/dev/null || { log "!! scp code failed"; api -X DELETE "$API/servers/$SID" >/dev/null; exit 1; }
ssh $SSHO root@"$IP" "bash -s" <<EOF 2>/dev/null
set -e
cd /root/minecraft-worldgen
tar xzf /tmp/rv_code.tgz
mkdir -p /root/dems
cp islands/prerot_dems/$DEMFILE /root/dems/ 2>/dev/null || echo "WARN: prerot DEM not in tarball"
/root/venv/bin/python - <<'PY'
import json,os
lay=json.load(open("islands/layout.json"))
for isl in lay["islands"]:
    bn=os.path.basename(isl["dem_path"].replace("\\\\","/"))
    isl["dem_path"]="/root/dems/"+bn
json.dump(lay,open("islands/layout.json","w"),indent=2)
print("layout rewritten")
PY
ls -la /root/dems/
rm -f /root/all_done
tmux kill-server 2>/dev/null || true
tmux new-session -d -s isl "cd /root/minecraft-worldgen && /root/venv/bin/python islands/_box_all_run.py $WORK $KEY > /root/run.log 2>&1"
echo launched
EOF
log "$NAME LAUNCHED (workers=$WORK key=$KEY)"

# ---- poll-collect-delete (1.5h cap) ----
while :; do
  if ssh $SSHO root@"$IP" "test -f /root/all_done" 2>/dev/null; then
    log "$NAME all_done; collecting"
    ssh $SSHO root@"$IP" "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz islands/out 2>/dev/null" 2>/dev/null
    if scp $SSHO -q root@"$IP":/tmp/out.tgz "$COL/grenada.tgz" 2>/dev/null && [ -s "$COL/grenada.tgz" ]; then
      ssh $SSHO root@"$IP" "cat /root/all_done" 2>/dev/null | while read -r ln; do log "    $ln"; done
      log "collected -> $COL/grenada.tgz ($(du -h "$COL/grenada.tgz"|cut -f1))"
      api -X DELETE "$API/servers/$SID" >/dev/null 2>&1; log "DELETED $NAME (id=$SID)"
      break
    else
      log "!! collect empty/failed; retry"
    fi
  fi
  if [ $(( $(date +%s) - START )) -gt 5400 ]; then
    log "!! 1.5h cap; deleting"; api -X DELETE "$API/servers/$SID" >/dev/null 2>&1; break
  fi
  sleep 30
done

# ---- safety sweep ----
rem=$(api "$API/servers" | "$PY" -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin).get('servers',[]) if s['name']=='isl-grenada']" 2>/dev/null)
for id in $rem; do api -X DELETE "$API/servers/$id" >/dev/null 2>&1; log "safety-deleted $id"; done
log "=== GRENADA DONE; elapsed $(( ($(date +%s)-START)/60 ))m ==="

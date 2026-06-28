#!/usr/bin/env bash
# S97 3-archipelago confirmation render — Grenada / Kostati / New Vincentia, one
# box each. Confirms the flow_erosion archipelago-flood fix in-world before any
# full 15-island render. Lifecycle = _cloud_render_s95.sh (create -> wait-ssh ->
# deploy code+DEMs -> tmux bake+render -> poll/watchdog -> collect -> DELETE).
#
# DEM handling (S97 fix): scp each island's ACTUAL layout dem_path -> /root/dems/
# <basename>, so Grenada's PRE-ROTATED DEM (islands/prerot_dems/12_445_prerot_
# 16bit.png) ships correctly (the old key-glob into ~/Downloads would have grabbed
# the wrong, un-rotated file). The on-box layout rewrite points every dem_path at
# /root/dems/<basename> to match.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
TGZ="/tmp/rv_code.tgz"
COL="$ROOT/islands/_collect_s97"; mkdir -p "$COL"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
KEYID=112518810
IMG=396927540
START=$(date +%s)
log(){ echo "[$(date +%H:%M:%S)] $*"; }

source "$ROOT/islands/box_watchdog.sh"
bash "$ROOT/islands/make_tarball.sh" || { log "!! make_tarball failed; aborting"; exit 1; }

# ---- per-box plan: one archipelago each ----
NAME=(isl-a1 isl-a2 isl-a3)
TYPE=(ccx63 ccx63 ccx63)
LOC=(fsn1 nbg1 hel1)
WORK=(44 44 44)
KEYS=("12_445" "13_130" "17_288")     # grenada / kostati / new_vincentia
declare -a SID IP DONE
N=3
ALLLOC=(fsn1 nbg1 hel1 hil)
api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }

# resolve an island's actual DEM file (git-bash path) from layout.json by key
dem_file(){ "$PY" "$ROOT/islands/_dem_gitpath.py" "$1"; }

# ---- 1. create ----
for i in $(seq 0 $((N-1))); do
  DONE[$i]=0; SID[$i]=""
  for loc in ${LOC[$i]} ${ALLLOC[*]}; do
    body=$(printf '{"name":"%s","server_type":"%s","image":%d,"location":"%s","ssh_keys":[%d],"start_after_create":true}' \
                  "${NAME[$i]}" "${TYPE[$i]}" "$IMG" "$loc" "$KEYID")
    resp=$(api -X POST "$API/servers" -d "$body")
    sid=$(echo "$resp" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('server',{}).get('id',''))" 2>/dev/null)
    if [ -n "$sid" ]; then SID[$i]=$sid; log "create ${NAME[$i]} @${loc} key=${KEYS[$i]} id=$sid"; break; fi
    log "  ${NAME[$i]}@${loc} create failed; next loc"
  done
  [ -z "${SID[$i]}" ] && log "  !! ${NAME[$i]} create FAILED all locations"
done

# ---- 2. wait running + IP ----
for i in $(seq 0 $((N-1))); do
  [ -z "${SID[$i]}" ] && continue
  for _ in $(seq 1 60); do
    r=$(api "$API/servers/${SID[$i]}")
    st=$(echo "$r" | "$PY" -c "import sys,json;s=json.load(sys.stdin)['server'];print(s['status'],s['public_net']['ipv4']['ip'])" 2>/dev/null)
    set -- $st; status="${1:-}"; ip="${2:-}"
    if [ "$status" = "running" ] && [ -n "$ip" ]; then IP[$i]=$ip; log "${NAME[$i]} running ip=$ip"; break; fi
    sleep 6
  done
  [ -z "${IP[$i]:-}" ] && log "  !! ${NAME[$i]} never reached running"
done

# ---- 3. deploy + launch ----
deploy(){
  local i=$1 ip=${IP[$i]:-}
  [ -z "$ip" ] && { log "  skip deploy ${NAME[$i]} (no ip)"; return; }
  local up=0
  for _ in $(seq 1 40); do ssh $SSHO root@"$ip" true 2>/dev/null && { up=1; break; }; sleep 6; done
  [ "$up" = 0 ] && { log "  !! ${NAME[$i]} ssh never up"; return; }
  log "  ${NAME[$i]} ssh up; pushing code+DEM"
  ssh $SSHO root@"$ip" "mkdir -p /root/dems" 2>/dev/null
  scp $SSHO -q "$TGZ" root@"$ip":/tmp/rv_code.tgz 2>/dev/null || { log "  !! ${NAME[$i]} scp code failed"; return; }
  local demf; demf=$(dem_file "${KEYS[$i]}")
  if [ -z "$demf" ] || [ ! -f "$demf" ]; then log "  !! ${NAME[$i]} DEM not found for ${KEYS[$i]} ($demf)"; return; fi
  scp $SSHO -q "$demf" root@"$ip":/root/dems/"$(basename "$demf")" 2>/dev/null || { log "  !! ${NAME[$i]} scp DEM failed"; return; }
  log "  ${NAME[$i]} DEM $(basename "$demf") pushed"
  ssh $SSHO root@"$ip" "bash -s" <<EOF 2>/dev/null
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
tmux new-session -d -s isl "cd /root/minecraft-worldgen && /root/venv/bin/python islands/_box_all_run.py ${WORK[$i]} ${KEYS[$i]} > /root/run.log 2>&1"
echo launched
EOF
  log "  ${NAME[$i]} LAUNCHED (key=${KEYS[$i]})"
  box_arm_selfdestruct "$ip" 180
}
for i in $(seq 0 $((N-1))); do deploy "$i" & done
wait
log "=== all boxes launched; poll-collect-delete (3h cap) ==="

# ---- 4. poll-collect-delete (with watchdog) ----
del_box(){ local i=$1; api -X DELETE "$API/servers/${SID[$i]}" >/dev/null 2>&1; log "  DELETED ${NAME[$i]} (id=${SID[$i]})"; }
while :; do
  alive=0
  for i in $(seq 0 $((N-1))); do
    [ "${DONE[$i]}" = 1 ] && continue
    [ -z "${IP[$i]:-}" ] && { DONE[$i]=1; continue; }
    alive=1; ip=${IP[$i]}
    case "$(box_state "$ip" 600)" in
      FAIL)  log "  !! ${NAME[$i]} EARLY-FAIL; saving log + deleting"
             ssh $SSHO root@"$ip" "cat /root/run.log" > "$COL/${NAME[$i]}.FAIL.log" 2>/dev/null
             del_box "$i"; DONE[$i]=1; continue;;
      STALL) log "  !! ${NAME[$i]} STALLED >10m; saving log + deleting"
             ssh $SSHO root@"$ip" "cat /root/run.log" > "$COL/${NAME[$i]}.STALL.log" 2>/dev/null
             del_box "$i"; DONE[$i]=1; continue;;
    esac
    if ssh $SSHO root@"$ip" "test -f /root/all_done" 2>/dev/null; then
      log "${NAME[$i]} all_done; collecting"
      ssh $SSHO root@"$ip" "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz islands/out 2>/dev/null" 2>/dev/null
      if scp $SSHO -q root@"$ip":/tmp/out.tgz "$COL/${NAME[$i]}.tgz" 2>/dev/null && [ -s "$COL/${NAME[$i]}.tgz" ]; then
        ssh $SSHO root@"$ip" "cat /root/all_done" 2>/dev/null | while read -r ln; do log "    ${NAME[$i]}: $ln"; done
        log "  collected ${NAME[$i]} ($(du -h "$COL/${NAME[$i]}.tgz"|cut -f1)); deleting"
        del_box "$i"; DONE[$i]=1
      else
        log "  !! ${NAME[$i]} collect empty/failed; retry next pass"
      fi
    fi
  done
  [ "$alive" = 0 ] && { log "=== all collected ==="; break; }
  if [ $(( $(date +%s) - START )) -gt 10800 ]; then
    log "!! 3h cap; force-deleting remaining"
    for i in $(seq 0 $((N-1))); do [ "${DONE[$i]}" = 1 ] || del_box "$i"; done; break
  fi
  sleep 45
done

# ---- 5. safety sweep ----
rem=$(api "$API/servers" | "$PY" -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin).get('servers',[]) if s['name'].startswith('isl-a')]" 2>/dev/null)
for id in $rem; do api -X DELETE "$API/servers/$id" >/dev/null 2>&1; log "safety-deleted $id"; done
log "=== DONE; tarballs in $COL ; elapsed $(( ($(date +%s)-START)/60 ))m ==="
log "verify 0 servers: $(api "$API/servers" | "$PY" -c "import sys,json;print(len(json.load(sys.stdin).get('servers',[])),'servers remain')")"

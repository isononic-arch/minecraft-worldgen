#!/usr/bin/env bash
# S95 polish re-render — 8-box Hetzner orchestration.
# Lifecycle: create -> wait-ssh -> deploy(code+DEMs) -> tmux bake+render ->
# poll /root/all_done -> scp out -> DELETE box on collect. 4h hard backstop.
# Gate box deletion on all_done + a VERIFIED local tarball (handoff lesson:
# NOT a timer). Logs to islands/_cloud_render_s95.log.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"
cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
DL="/c/Users/nicho/Downloads"
TGZ="/tmp/rv_code.tgz"
COL="$ROOT/islands/_collect"; mkdir -p "$COL"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
KEYID=112518810
IMG=396927540
START=$(date +%s)
log(){ echo "[$(date +%H:%M:%S)] $*"; }

# S97: deterministic code bundle (include-list, asserts DEMs present) + the
# early-failure/idle watchdog. Replaces the hand-typed `tar` whose recursive
# --exclude silently dropped the prerot DEM and idled a box an hour.
source "$ROOT/islands/box_watchdog.sh"
bash "$ROOT/islands/make_tarball.sh" || { log "!! make_tarball failed; aborting"; exit 1; }

# ---- per-box plan (index 0..7) ----
NAME=(isl-b1 isl-b2 isl-b3 isl-b4 isl-b5 isl-b6 isl-b7 isl-b8)
TYPE=(ccx63 ccx63 ccx63 ccx63 ccx63 ccx63 ccx63 ccx63)  # snapshot needs 960GB disk -> ccx63 only
LOC=(fsn1 nbg1 hel1 hil fsn1 nbg1 hel1 hil)
WORK=(44 44 44 44 44 44 44 44)                           # 48-core boxes, ~2GB/tile, 192GB RAM
KEYS=("-17_622" "11_060" "-50_393" "-1_509,21_395" "13_130,11_863" "17_288,49_722" "23_887,18_299,12_445" "-20_529,10_941,-21_008")
declare -a SID IP DONE
N=8

api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }

# ---- 1. create (with location fallback on capacity errors) ----
ALLLOC=(fsn1 nbg1 hel1 hil)
for i in $(seq 0 $((N-1))); do
  DONE[$i]=0; SID[$i]=""
  # try assigned loc first, then the rest
  trylocs="${LOC[$i]} ${ALLLOC[*]}"
  for loc in $trylocs; do
    body=$(printf '{"name":"%s","server_type":"%s","image":%d,"location":"%s","ssh_keys":[%d],"start_after_create":true}' \
                  "${NAME[$i]}" "${TYPE[$i]}" "$IMG" "$loc" "$KEYID")
    resp=$(api -X POST "$API/servers" -d "$body")
    sid=$(echo "$resp" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('server',{}).get('id',''))" 2>/dev/null)
    if [ -n "$sid" ]; then
      SID[$i]=$sid; log "create ${NAME[$i]} ${TYPE[$i]}@${loc} keys=${KEYS[$i]} -> id=$sid"; break
    fi
    err=$(echo "$resp" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('error',{}).get('code',''))" 2>/dev/null)
    log "  create ${NAME[$i]}@${loc} failed ($err); trying next loc"
  done
  [ -z "${SID[$i]}" ] && log "  !! ${NAME[$i]} create FAILED in all locations"
done

# ---- 2. wait for running + IP ----
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

# ---- 3. deploy + launch (parallel per box) ----
deploy(){
  local i=$1 ip=${IP[$i]:-}
  [ -z "$ip" ] && { log "  skip deploy ${NAME[$i]} (no ip)"; return; }
  # wait sshd
  local up=0
  for _ in $(seq 1 40); do
    if ssh $SSHO root@"$ip" true 2>/dev/null; then up=1; break; fi
    sleep 6
  done
  [ "$up" = 0 ] && { log "  !! ${NAME[$i]} ssh never came up"; return; }
  log "  ${NAME[$i]} ssh up; pushing code+DEMs"
  ssh $SSHO root@"$ip" "mkdir -p /root/dems" 2>/dev/null
  scp $SSHO -q "$TGZ" root@"$ip":/tmp/rv_code.tgz 2>/dev/null || { log "  !! ${NAME[$i]} scp code failed"; return; }
  # DEMs for this box's keys
  IFS=',' read -ra ks <<< "${KEYS[$i]}"
  for k in "${ks[@]}"; do
    f=$(ls "$DL/${k}_"*_16bit.png 2>/dev/null | head -1)
    [ -z "$f" ] && { log "  !! ${NAME[$i]} DEM missing for $k"; continue; }
    scp $SSHO -q "$f" root@"$ip":/root/dems/ 2>/dev/null || log "  !! ${NAME[$i]} scp DEM $k failed"
  done
  # extract code, rewrite layout dem_paths, launch in tmux
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
  log "  ${NAME[$i]} LAUNCHED (workers=${WORK[$i]} keys=${KEYS[$i]})"
  box_arm_selfdestruct "$ip" 180   # S97: box can't outlive 3h even if orchestrator dies
}
for i in $(seq 0 $((N-1))); do deploy "$i" & done
wait
log "=== all boxes launched; entering poll-collect-delete (3h cap) ==="

# ---- 4. poll-collect-delete ----
del_box(){ local i=$1; api -X DELETE "$API/servers/${SID[$i]}" >/dev/null 2>&1; log "  DELETED ${NAME[$i]} (id=${SID[$i]})"; }
while :; do
  alive=0
  for i in $(seq 0 $((N-1))); do
    [ "${DONE[$i]}" = 1 ] && continue
    [ -z "${IP[$i]:-}" ] && { DONE[$i]=1; continue; }
    alive=1
    ip=${IP[$i]}
    # S97 watchdog: reap a crashed/stalled box immediately instead of idling to
    # the 3h cap (the S96 FileNotFoundError-idled-an-hour scenario).
    case "$(box_state "$ip" 600)" in
      FAIL)
        log "  !! ${NAME[$i]} EARLY-FAIL (run.log crash signature); saving log + deleting"
        ssh $SSHO root@"$ip" "cat /root/run.log" > "$COL/${NAME[$i]}.FAIL.log" 2>/dev/null
        del_box "$i"; DONE[$i]=1; continue;;
      STALL)
        log "  !! ${NAME[$i]} STALLED (no run.log progress >10m); saving log + deleting"
        ssh $SSHO root@"$ip" "cat /root/run.log" > "$COL/${NAME[$i]}.STALL.log" 2>/dev/null
        del_box "$i"; DONE[$i]=1; continue;;
    esac
    if ssh $SSHO root@"$ip" "test -f /root/all_done" 2>/dev/null; then
      log "${NAME[$i]} all_done; collecting"
      ssh $SSHO root@"$ip" "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz islands/out 2>/dev/null" 2>/dev/null
      if scp $SSHO -q root@"$ip":/tmp/out.tgz "$COL/${NAME[$i]}.tgz" 2>/dev/null && [ -s "$COL/${NAME[$i]}.tgz" ]; then
        sz=$(du -h "$COL/${NAME[$i]}.tgz"|cut -f1)
        ssh $SSHO root@"$ip" "cat /root/all_done" 2>/dev/null | while read -r ln; do log "    ${NAME[$i]}: $ln"; done
        log "  collected ${NAME[$i]} -> ${NAME[$i]}.tgz ($sz)"
        del_box "$i"; DONE[$i]=1
      else
        log "  !! ${NAME[$i]} collect tarball empty/failed; will retry"
      fi
    fi
  done
  [ "$alive" = 0 ] && { log "=== all boxes collected ==="; break; }
  # 4h hard backstop
  if [ $(( $(date +%s) - START )) -gt 10800 ]; then
    log "!! 3h cap hit; force-deleting any remaining boxes"
    for i in $(seq 0 $((N-1))); do [ "${DONE[$i]}" = 1 ] || del_box "$i"; done
    break
  fi
  sleep 45
done

# ---- 5. final safety sweep: delete any isl-b* still alive ----
rem=$(api "$API/servers" | "$PY" -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin).get('servers',[]) if s['name'].startswith('isl-b')]" 2>/dev/null)
for id in $rem; do api -X DELETE "$API/servers/$id" >/dev/null 2>&1; log "safety-deleted leftover server $id"; done
log "=== DONE. collected tarballs in $COL ; elapsed $(( ($(date +%s)-START)/60 ))m ==="
log "verify 0 servers: $(api "$API/servers" | "$PY" -c "import sys,json;print(len(json.load(sys.stdin).get('servers',[])),'servers remain')")"

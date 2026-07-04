#!/usr/bin/env bash
# _cloud_render_v11_adopt.sh — ADOPT the 8 already-created v11-b* boxes (the
# original dispatch's orchestrator was killed by a bad `&`/sleep wrapper right
# after `create`, before deploy). Skips creation; reuses V11's deploy + poll-
# collect-delete against the running boxes so the ~8 already-billed box-hours
# aren't wasted. Boxes bake fresh (new beach+splines) + render (kelp+tree fixes).
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
TGZ="/tmp/rv_code.tgz"                        # already built by the original run
COL="$ROOT/islands/_collect_v11"; mkdir -p "$COL"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
START=$(date +%s); log(){ echo "[$(date +%H:%M:%S)] $*"; }
api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }
source "$ROOT/islands/box_watchdog.sh"
dem_file(){ "$PY" "$ROOT/islands/_dem_gitpath.py" "$1"; }

# rebuild the tarball if missing (idempotent, matches make_tarball include-list)
[ -f "$TGZ" ] || TGZ="$TGZ" bash "$ROOT/islands/make_tarball.sh" || { log "!! tarball missing + rebuild failed"; exit 1; }

# ---- the 8 running boxes (from the killed dispatch) ----
NAME=(v11-b1 v11-b2 v11-b3 v11-b4 v11-b5 v11-b6 v11-b7 v11-b8)
SID=(147767449 147767450 147767453 147767457 147767466 147767474 147767486 147767490)
IP=(49.13.73.16 88.99.173.164 37.27.41.207 5.78.119.14 167.233.227.205 46.225.104.70 77.42.126.32 5.78.190.160)
KEYS=("-17_622" "11_060" "-50_393" "-1_509,21_395" "13_130,11_863" "17_288,49_722" "23_887,18_299,12_445" "-20_529,10_941,-21_008")
WORK=(44 44 44 44 44 44 44 44)
N=8; declare -a DONE; for i in $(seq 0 $((N-1))); do DONE[$i]=0; done

deploy(){
  local i=$1 ip=${IP[$i]:-}
  local up=0
  for _ in $(seq 1 50); do ssh $SSHO root@"$ip" true 2>/dev/null && { up=1; break; }; sleep 6; done
  [ "$up" = 0 ] && { log "  !! ${NAME[$i]} ssh never up"; return; }
  log "  ${NAME[$i]} ssh up; pushing code+DEMs"
  ssh $SSHO root@"$ip" "mkdir -p /root/dems" 2>/dev/null
  scp $SSHO -q "$TGZ" root@"$ip":/tmp/rv_code.tgz 2>/dev/null || { log "  !! ${NAME[$i]} scp code failed"; return; }
  IFS=',' read -ra ks <<< "${KEYS[$i]}"
  for k in "${ks[@]}"; do
    local demf; demf=$(dem_file "$k")
    [ -f "$demf" ] || { log "  !! ${NAME[$i]} DEM missing $k"; continue; }
    scp $SSHO -q "$demf" root@"$ip":/root/dems/"$(basename "$demf")" 2>/dev/null \
        && log "  ${NAME[$i]} DEM $(basename "$demf") pushed" || log "  !! ${NAME[$i]} scp DEM $k failed"
  done
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
  log "  ${NAME[$i]} LAUNCHED (keys=${KEYS[$i]})"
  box_arm_selfdestruct "$ip" 180
}
for i in $(seq 0 $((N-1))); do deploy "$i" & done
wait
log "=== all boxes launched; poll-collect-delete (3h cap) ==="

del_box(){ local i=$1; api -X DELETE "$API/servers/${SID[$i]}" >/dev/null 2>&1; log "  DELETED ${NAME[$i]} (id=${SID[$i]})"; }
while :; do
  alive=0
  for i in $(seq 0 $((N-1))); do
    [ "${DONE[$i]}" = 1 ] && continue
    alive=1; ip=${IP[$i]}
    case "$(box_state "$ip" 900)" in
      FAIL)  log "  !! ${NAME[$i]} EARLY-FAIL"; ssh $SSHO root@"$ip" "cat /root/run.log" > "$COL/${NAME[$i]}.FAIL.log" 2>/dev/null; del_box "$i"; DONE[$i]=1; continue;;
      STALL) log "  !! ${NAME[$i]} STALLED"; ssh $SSHO root@"$ip" "cat /root/run.log" > "$COL/${NAME[$i]}.STALL.log" 2>/dev/null; del_box "$i"; DONE[$i]=1; continue;;
    esac
    if ssh $SSHO root@"$ip" "test -f /root/all_done" 2>/dev/null; then
      log "${NAME[$i]} all_done; collecting"
      ssh $SSHO root@"$ip" "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz islands/out 2>/dev/null" 2>/dev/null
      if scp $SSHO -q root@"$ip":/tmp/out.tgz "$COL/${NAME[$i]}.tgz" 2>/dev/null && [ -s "$COL/${NAME[$i]}.tgz" ]; then
        ssh $SSHO root@"$ip" "cat /root/all_done" 2>/dev/null | while read -r ln; do log "    ${NAME[$i]}: $ln"; done
        log "  collected ${NAME[$i]} ($(du -h "$COL/${NAME[$i]}.tgz"|cut -f1)); deleting"; del_box "$i"; DONE[$i]=1
      else log "  !! ${NAME[$i]} collect empty; retry"; fi
    fi
  done
  [ "$alive" = 0 ] && { log "=== all collected ==="; break; }
  [ $(( $(date +%s) - START )) -gt 10800 ] && { log "!! 3h cap; force-delete"; for i in $(seq 0 $((N-1))); do [ "${DONE[$i]}" = 1 ] || del_box "$i"; done; break; }
  sleep 45
done
rem=$(api "$API/servers" | "$PY" -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin).get('servers',[]) if s['name'].startswith('v11-b')]" 2>/dev/null)
for id in $rem; do api -X DELETE "$API/servers/$id" >/dev/null 2>&1; log "safety-deleted $id"; done
log "=== DONE tarballs in $COL elapsed $(( ($(date +%s)-START)/60 ))m ==="
log "verify: $(api "$API/servers" | "$PY" -c "import sys,json;print(len(json.load(sys.stdin).get('servers',[])),'servers remain')")"

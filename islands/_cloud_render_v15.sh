#!/usr/bin/env bash
# S105 V15 FULL render — all 15 islands, 8 boxes (proven V7.1/V11 allocation,
# Madre isolated). Carries the S105 beach fix (depth-gated pond anchor, width 24,
# shore-strip windthrow claim) via the on-box bake from the tarballed local code.
#
# DISPATCH-ONLY (differs from V11 by design): this script creates + deploys +
# launches, arms box_guard (TTL labels + on-box self-DELETE via metadata API),
# writes cloud_bake/runspec_v15.json — then EXITS. The poll/collect/verify/delete
# phase is cloud_bake/render_monitor.py, launched separately against that runspec.
# Exactly ONE babysitter touches the boxes after launch (V11's in-script loop had
# the unbounded collect-retry that idled a box to the 3h cap — S104).
#
# Collect (done by the monitor) now tarballs islands/masks_islands TOO, so the
# local mask dir becomes authoritative again (S105 finding: 12/15 stale locally).
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
TGZ="/tmp/rv_code.tgz"
COL="$ROOT/islands/_collect_v15"; mkdir -p "$COL"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
KEYID=112518810
IMG=396927540
TTL_MIN=150
START=$(date +%s)
log(){ echo "[$(date +%H:%M:%S)] $*"; }

bash "$ROOT/islands/make_tarball.sh" || { log "!! make_tarball failed; aborting"; exit 1; }

# ---- 8-box plan (V7.1/V11 allocation; all 15 islands; Madre alone) ----
NAME=(v15-b1 v15-b2 v15-b3 v15-b4 v15-b5 v15-b6 v15-b7 v15-b8)
TYPE=(ccx63 ccx63 ccx63 ccx63 ccx63 ccx63 ccx63 ccx63)
LOC=(fsn1 nbg1 hel1 hil fsn1 nbg1 hel1 hil)
WORK=(44 44 44 44 44 44 44 44)
KEYS=("-17_622" "11_060" "-50_393" "-1_509,21_395" "13_130,11_863" "17_288,49_722" "23_887,18_299,12_445" "-20_529,10_941,-21_008")
declare -a SID IP
N=8
ALLLOC=(fsn1 nbg1 hel1 hil)
api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }
dem_file(){ "$PY" "$ROOT/islands/_dem_gitpath.py" "$1"; }

# ---- 0. guard armed BEFORE any box exists ----
"$PY" "$ROOT/cloud_bake/box_guard.py" arm
"$PY" "$ROOT/cloud_bake/box_guard.py" status

# ---- 1. create (loc fallback on capacity) ----
for i in $(seq 0 $((N-1))); do
  SID[$i]=""
  for loc in ${LOC[$i]} ${ALLLOC[*]}; do
    body=$(printf '{"name":"%s","server_type":"%s","image":%d,"location":"%s","ssh_keys":[%d],"start_after_create":true,"labels":{"ttl_min":"%d"}}' \
                  "${NAME[$i]}" "${TYPE[$i]}" "$IMG" "$loc" "$KEYID" "$TTL_MIN")
    resp=$(api -X POST "$API/servers" -d "$body")
    sid=$(echo "$resp" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('server',{}).get('id',''))" 2>/dev/null)
    if [ -n "$sid" ]; then SID[$i]=$sid; log "create ${NAME[$i]} @${loc} keys=${KEYS[$i]} id=$sid ttl=${TTL_MIN}m"; break; fi
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

# ---- 3. deploy + launch (parallel; same proven V11 heredoc) ----
deploy(){
  local i=$1 ip=${IP[$i]:-}
  [ -z "$ip" ] && { log "  skip deploy ${NAME[$i]} (no ip)"; return; }
  local up=0
  for _ in $(seq 1 40); do ssh $SSHO root@"$ip" true 2>/dev/null && { up=1; break; }; sleep 6; done
  [ "$up" = 0 ] && { log "  !! ${NAME[$i]} ssh never up"; return; }
  log "  ${NAME[$i]} ssh up; pushing code+DEMs"
  ssh $SSHO root@"$ip" "mkdir -p /root/dems" 2>/dev/null
  scp $SSHO -q "$TGZ" root@"$ip":/tmp/rv_code.tgz 2>/dev/null || { log "  !! ${NAME[$i]} scp code failed"; return; }
  IFS=',' read -ra ks <<< "${KEYS[$i]}"
  for k in "${ks[@]}"; do
    local demf; demf=$(dem_file "$k")
    if [ -z "$demf" ] || [ ! -f "$demf" ]; then log "  !! ${NAME[$i]} DEM not found for $k ($demf)"; continue; fi
    scp $SSHO -q "$demf" root@"$ip":/root/dems/"$(basename "$demf")" 2>/dev/null \
        && log "  ${NAME[$i]} DEM $(basename "$demf") pushed" \
        || log "  !! ${NAME[$i]} scp DEM $k failed"
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
tmux new-session -d -s isl "cd /root/minecraft-worldgen && /root/venv/bin/python islands/_box_all_run.py ${WORK[$i]} ${KEYS[$i]} > /root/run.log 2>&1"  # lint-ok proven V11 pattern: WORK/KEYS expand locally BY DESIGN
echo launched
EOF
  log "  ${NAME[$i]} LAUNCHED (keys=${KEYS[$i]})"
  # billing-stop backstop even if this PC dies: on-box self-DELETE via metadata API
  "$PY" "$ROOT/cloud_bake/box_guard.py" selfdestruct "$ip" --minutes $TTL_MIN --delete-via-api
}
for i in $(seq 0 $((N-1))); do deploy "$i" & done
wait
log "=== all boxes launched ==="

# ---- 4. write the monitor runspec, then EXIT (monitor takes over) ----
{
  for i in $(seq 0 $((N-1))); do
    [ -z "${SID[$i]}" ] && continue
    echo "${NAME[$i]} ${SID[$i]} ${IP[$i]:-} ${WORK[$i]} ${KEYS[$i]}"
  done
} > "$COL/boxes.txt"
"$PY" - "$COL/boxes.txt" <<'PYEOF'
import json, sys
from pathlib import Path
ROOT = Path(r"C:/Users/nicho/minecraft-worldgen")
sys.path.insert(0, str(ROOT))
from islands.render_islands import safe_name
lay = json.loads((ROOT/"islands"/"layout.json").read_text())["islands"]
own = json.loads((ROOT/"islands"/"region_ownership_s101.json").read_text())["islands"]
def key_of(e):
    import re, os
    bn = os.path.basename(e["dem_path"].replace("\\", "/"))
    m = re.match(r"(-?\d+)_(-?\d+)_", bn)
    return f"{m.group(1)}_{m.group(2)}" if m else None
key2name = {key_of(e): safe_name(e["name"]) for e in lay}
boxes = []
for line in Path(sys.argv[1]).read_text().splitlines():
    name, sid, ip, work, keys = line.split()
    units = keys.split(",")
    exp = 0
    for k in units:
        nm = key2name.get(k)
        exp += len(own.get(nm, [])) if nm else 0
    boxes.append({
        "name": name, "id": int(sid), "ip": ip,
        "flag": "/root/all_done", "log": "/root/run.log",
        "collect": "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz islands/out islands/masks_islands",
        "remote_tar": "/tmp/out.tgz",
        "job_restart": f"cd /root/minecraft-worldgen && tmux new-session -d -s isl \"/root/venv/bin/python islands/_box_all_run.py {work} {keys} > /root/run.log 2>&1\"",
        "work_units": units,
        "min_regions": max(1, int(0.8 * exp)),
    })
spec = {
    "run_name": "v15", "kind": "islands",
    "ttl_min": 120, "wall_cap_min": 150,
    "stall_secs": 900, "poll_secs": 45, "collect_retries": 3, "unreach_grace": 4,
    "collect_dir": "islands/_collect_v15",
    "boxes": boxes,
}
out = ROOT/"cloud_bake"/"runspec_v15.json"
out.write_text(json.dumps(spec, indent=1))
print(f"runspec written: {out} ({len(boxes)} boxes, expected regions per box: "
      f"{[ (b['name'], b['min_regions']) for b in boxes ]})")
PYEOF
log "=== DISPATCH DONE in $(( ($(date +%s)-START)/60 ))m — launch the monitor: ==="
log "  \"$PY\" cloud_bake/render_monitor.py cloud_bake/runspec_v15.json"

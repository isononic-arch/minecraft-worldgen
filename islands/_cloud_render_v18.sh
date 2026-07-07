#!/usr/bin/env bash
# S107 V18 FULL island render — all 15 islands, 8 boxes (proven V7.1/V11/V17
# allocation, Madre isolated) — BOX-DIRECT-TO-BLOOMHOST.
#
# = _cloud_render_v17.sh (on-box bake from tarballed local code, box_guard TTL +
# on-box self-destruct, DISPATCH-ONLY with render_monitor as the ONE babysitter)
# PLUS the S107 push flow:
#   _box_all_run.py (bake+render, writes /root/all_done)
#     && box_push.py --src islands/out --recursive --wait-go
#          (gate .go is already open — mainland phase opened it; contested
#           island-overlap regions are EXCLUDED and travel home in the collect
#           tarball for chunk-merge + finalize upload)
#     && touch /root/push_done          <- monitor flag (RENDERED+PUSHED)
#
# Refire subset:  SUBSET="3" bash islands/_cloud_render_v18.sh   (box indices 1-8)
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
TGZ="/tmp/rv_code.tgz"
COL="$ROOT/islands/_collect_v18"; mkdir -p "$COL"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
KEYID=112518810
IMG=396927540
# Islands wall ~75m (Madre 68m long pole; gate already open so push is immediate).
# 140m TTL = ~2x margin AND caps billing: 8 boxes x 140m x E1.6138/h = ~E30.
# Monitor reaps at ttl 130m (below this). Env-overridable so the driver can pass
# a tight TTL_MIN=90 for a surgical SUBSET refire.
TTL_MIN="${TTL_MIN:-140}"
CREDS_LOCAL="/c/Users/nicho/.bloom_creds.json"
CONTESTED="r.95.103.mca,r.96.103.mca,r.97.122.mca,r.97.123.mca,r.97.124.mca,r.60.101.mca,r.60.102.mca,r.60.103.mca,r.60.104.mca,r.100.114.mca"
START=$(date +%s)
log(){ echo "[$(date +%H:%M:%S)] $*"; }
[ -f "$CREDS_LOCAL" ] || { log "FATAL: $CREDS_LOCAL missing"; exit 2; }

bash "$ROOT/islands/make_tarball.sh" || { log "!! make_tarball failed; aborting"; exit 1; }

# ---- 8-box plan (V7.1/V11/V17 allocation; all 15 islands; Madre alone b3) ----
NAME=(v18-b1 v18-b2 v18-b3 v18-b4 v18-b5 v18-b6 v18-b7 v18-b8)
TYPE=(ccx63 ccx63 ccx63 ccx63 ccx63 ccx63 ccx63 ccx63)
LOC=(fsn1 nbg1 hel1 hil fsn1 nbg1 hel1 hil)
WORK=(44 44 44 44 44 44 44 44)
KEYS=("-17_622" "11_060" "-50_393" "-1_509,21_395" "13_130,11_863" "17_288,49_722" "23_887,18_299,12_445" "-20_529,10_941,-21_008")
declare -a SID IP
N=8
SUBSET="${SUBSET:-}"   # e.g. "3" or "1,4" (1-based box numbers) for refires
in_subset(){ [ -z "$SUBSET" ] && return 0; echo ",$SUBSET," | grep -q ",$(( $1 + 1 )),"; }
ALLLOC=(fsn1 nbg1 hel1 hil)
api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }
dem_file(){ "$PY" "$ROOT/islands/_dem_gitpath.py" "$1"; }

# expected pushed-region count for a key list (ownership manifest, 0.8 slack,
# contested excluded from push so they don't count)
minc_for(){ "$PY" - "$1" <<'PYEOF'
import json, os, re, sys
sys.path.insert(0, r"C:/Users/nicho/minecraft-worldgen")
from islands.render_islands import safe_name
ROOT = r"C:/Users/nicho/minecraft-worldgen"
lay = json.load(open(ROOT + "/islands/layout.json"))["islands"]
own = json.load(open(ROOT + "/islands/region_ownership_s101.json"))["islands"]
contested = {(95,103),(96,103),(97,122),(97,123),(97,124),(60,101),(60,102),(60,103),(60,104),(100,114)}
def key_of(e):
    bn = os.path.basename(e["dem_path"].replace("\\", "/"))
    m = re.match(r"(-?\d+)_(-?\d+)_", bn)
    return f"{m.group(1)}_{m.group(2)}" if m else None
k2n = {key_of(e): safe_name(e["name"]) for e in lay}
exp = set()
for k in sys.argv[1].split(","):
    for x, z in own.get(k2n.get(k, ""), []):
        if (x, z) not in contested:
            exp.add((x, z))
print(max(1, int(0.8 * len(exp))))
PYEOF
}

# ---- 0. guard armed BEFORE any box exists ----
"$PY" "$ROOT/cloud_bake/box_guard.py" arm
"$PY" "$ROOT/cloud_bake/box_guard.py" status

# ---- 1. create (loc fallback on capacity) ----
for i in $(seq 0 $((N-1))); do
  SID[$i]=""
  in_subset "$i" || continue
  for loc in ${LOC[$i]} ${ALLLOC[*]}; do
    body=$(printf '{"name":"%s","server_type":"%s","image":%d,"location":"%s","ssh_keys":[%d],"start_after_create":true,"labels":{"ttl_min":"%d"}}' \
                  "${NAME[$i]}" "${TYPE[$i]}" "$IMG" "$loc" "$KEYID" "$TTL_MIN")
    resp=$(api -X POST "$API/servers" -d "$body")
    sid=$(echo "$resp" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('server',{}).get('id',''))" 2>/dev/null)
    if [ -n "$sid" ]; then SID[$i]=$sid; log "create ${NAME[$i]} @${loc} keys=${KEYS[$i]} id=$sid ttl=${TTL_MIN}m"; break; fi
    log "  ${NAME[$i]}@${loc} create failed; next loc"
  done
  [ -z "${SID[$i]}" ] && log "  !! ${NAME[$i]} create FAILED all locations"
  [ -n "${SID[$i]}" ] && echo "$(date +%s) CREATE ${NAME[$i]} ${SID[$i]} ccx63" >> "$COL/boxes_log.txt"
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

# ---- 3. deploy + launch (parallel; proven V11 heredoc + S107 push chain) ----
deploy(){
  local i=$1 ip=${IP[$i]:-}
  [ -z "$ip" ] && { log "  skip deploy ${NAME[$i]} (no ip)"; return; }
  local up=0
  for _ in $(seq 1 40); do ssh $SSHO root@"$ip" true 2>/dev/null && { up=1; break; }; sleep 6; done
  [ "$up" = 0 ] && { log "  !! ${NAME[$i]} ssh never up"; return; }
  log "  ${NAME[$i]} ssh up; pushing code+DEMs+creds"
  ssh $SSHO root@"$ip" "mkdir -p /root/dems" 2>/dev/null
  scp $SSHO -q "$TGZ" root@"$ip":/tmp/rv_code.tgz 2>/dev/null || { log "  !! ${NAME[$i]} scp code failed"; return; }
  scp $SSHO -q "$CREDS_LOCAL" root@"$ip":/root/bloom_creds.json 2>/dev/null || { log "  !! ${NAME[$i]} scp creds failed"; return; }
  IFS=',' read -ra ks <<< "${KEYS[$i]}"
  for k in "${ks[@]}"; do
    local demf; demf=$(dem_file "$k")
    if [ -z "$demf" ] || [ ! -f "$demf" ]; then log "  !! ${NAME[$i]} DEM not found for $k ($demf)"; continue; fi
    scp $SSHO -q "$demf" root@"$ip":/root/dems/"$(basename "$demf")" 2>/dev/null \
        && log "  ${NAME[$i]} DEM $(basename "$demf") pushed" \
        || log "  !! ${NAME[$i]} scp DEM $k failed"
  done
  local minc; minc=$(minc_for "${KEYS[$i]}")
  log "  ${NAME[$i]} min-count=$minc"
  ssh $SSHO root@"$ip" "bash -s" <<EOF 2>/dev/null
set -e
cd /root/minecraft-worldgen
tar xzf /tmp/rv_code.tgz
/root/venv/bin/pip install -q paramiko
/root/venv/bin/python - <<'PY'
import json,os
lay=json.load(open("islands/layout.json"))
for isl in lay["islands"]:
    bn=os.path.basename(isl["dem_path"].replace("\\\\","/"))
    isl["dem_path"]="/root/dems/"+bn
json.dump(lay,open("islands/layout.json","w"),indent=2)
print("layout rewritten")
PY
rm -f /root/all_done /root/push_done
tmux kill-server 2>/dev/null || true
tmux new-session -d -s isl "cd /root/minecraft-worldgen && /root/venv/bin/python islands/_box_all_run.py ${WORK[$i]} ${KEYS[$i]} > /root/run.log 2>&1 && /root/venv/bin/python cloud_bake/box_push.py --creds /root/bloom_creds.json --src islands/out --recursive --min-count $minc --exclude $CONTESTED --wait-go --gate-timeout-min 45 >> /root/run.log 2>&1 && touch /root/push_done"  # lint-ok proven V11 pattern: WORK/KEYS/minc/CONTESTED expand locally BY DESIGN
echo launched
EOF
  log "  ${NAME[$i]} LAUNCHED (keys=${KEYS[$i]}, push-gated)"
  # billing-stop backstop even if this PC dies: on-box self-DELETE via metadata API
  "$PY" "$ROOT/cloud_bake/box_guard.py" selfdestruct "$ip" --minutes $TTL_MIN --delete-via-api
}
for i in $(seq 0 $((N-1))); do in_subset "$i" && deploy "$i" & done
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
CONTESTED = {(95,103),(96,103),(97,122),(97,123),(97,124),(60,101),(60,102),(60,103),(60,104),(100,114)}
CONTESTED_ARG = ",".join(f"r.{x}.{z}.mca" for x, z in sorted(CONTESTED))
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
    exp, exp_names = set(), []
    for k in units:
        nm = key2name.get(k)
        for x, z in own.get(nm, []) if nm else []:
            if (x, z) not in CONTESTED:
                exp.add((x, z))
    exp_names = sorted(f"r.{x}.{z}.mca" for x, z in exp)
    minc = max(1, int(0.8 * len(exp)))
    chain = (f"cd /root/minecraft-worldgen && /root/venv/bin/python islands/_box_all_run.py {work} {keys} > /root/run.log 2>&1"
             f" && /root/venv/bin/python cloud_bake/box_push.py --creds /root/bloom_creds.json --src islands/out --recursive"
             f" --min-count {minc} --exclude {CONTESTED_ARG} --wait-go --gate-timeout-min 45 >> /root/run.log 2>&1"
             f" && touch /root/push_done")
    boxes.append({
        "name": name, "id": int(sid), "ip": ip,
        "flag": "/root/push_done", "log": "/root/run.log",
        "collect": "/root/venv/bin/python /root/minecraft-worldgen/cloud_bake/box_collect_s107.py",
        "remote_tar": "/tmp/out.tgz",
        "verify": "push_manifest",
        "job_restart": f"tmux new-session -d -s isl \"{chain}\"",
        "work_units": units,
        "expected_regions": exp_names,
        "min_regions": minc,
    })
spec = {
    "run_name": "v18", "kind": "islands",
    "ttl_min": 130, "wall_cap_min": 145,
    "stall_secs": 900, "poll_secs": 45, "collect_retries": 3, "unreach_grace": 4,
    "collect_dir": "islands/_collect_v18",
    "boxes": boxes,
}
out = ROOT/"cloud_bake"/"runspec_v18.json"
out.write_text(json.dumps(spec, indent=1))
print(f"runspec written: {out} ({len(boxes)} boxes, min_regions: "
      f"{[(b['name'], b['min_regions']) for b in boxes]})")
PYEOF
log "=== DISPATCH DONE in $(( ($(date +%s)-START)/60 ))m — launch the monitor: ==="
log "  \"$PY\" cloud_bake/render_monitor.py cloud_bake/runspec_v18.json"

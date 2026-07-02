#!/usr/bin/env bash
# _cloud_render_options.sh — spin ONE ccx63 box, render 4 semi-arid bush-density
# options of tile (35,79) side-by-side (world-offset to adjacent tiles), collect,
# install to Vandirtest10. Box SELF-DESTRUCTS at TTL_MIN (default 60) — plus a
# detached LOCAL backup killer. Uploads full local S99 code (tarball) + the
# sandbox override so the box runs OUR code/biomes, not the snapshot's.
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT"
TOKEN_FILE="/c/Users/nicho/.hetzner_token"
SNAPSHOT_ID="${SNAPSHOT_ID:-396927540}"; LOC="${LOC:-fsn1}"
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
TTL_MIN="${TTL_MIN:-60}"
DEST="C:/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region"
OUT="/d/render_options"; rm -rf "$OUT"; mkdir -p "$OUT"
TOKEN=$(cat "$TOKEN_FILE"); hz(){ curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }
say(){ echo "[opts] $*"; }

# AMP -> world-tile-offset k (side-by-side east of 35,79): 2.0@35, 1.3@36, 0.9@37, 0.6@38
OPTS=("2.0:0" "1.3:1" "0.9:2" "0.6:3")

# 1) bundle local code (+ _render_opt.py) and check the override exists
[ -f _s99val_masks/override.tif ] || { echo "FATAL: _s99val_masks/override.tif missing"; exit 2; }
say "building code tarball..."
bash islands/make_tarball.sh || { echo "FATAL tarball"; exit 2; }   # -> /tmp/rv_code.tgz (driver _render_opt.py scp'd separately below)

# 2) spin one box
KEY_IDS=$(hz "$API/ssh_keys" | "$PY" -c "import json,sys;print(','.join(str(k['id']) for k in json.load(sys.stdin)['ssh_keys']))")
resp=$(hz -X POST "$API/servers" -d "{\"name\":\"vandir-opts\",\"server_type\":\"ccx63\",\"image\":$SNAPSHOT_ID,\"location\":\"$LOC\",\"ssh_keys\":[$KEY_IDS],\"start_after_create\":true}")
ID=$(echo "$resp"|"$PY" -c "import json,sys;print(json.load(sys.stdin).get('server',{}).get('id',''))")
IP=$(echo "$resp"|"$PY" -c "import json,sys;print(json.load(sys.stdin).get('server',{}).get('public_net',{}).get('ipv4',{}).get('ip',''))")
[ -n "$ID" ] && [ -n "$IP" ] || { echo "FATAL create: $resp"; exit 2; }
echo "$ID $IP" > "$OUT/box.txt"
say "BOX_ID=$ID BOX_IP=$IP  (self-destruct ${TTL_MIN}m)"

# 3) LOCAL backup killswitch (detached) — deletes the box at TTL even if the box hangs
nohup bash -c "sleep $((TTL_MIN*60)); curl -s -X DELETE -H 'Authorization: Bearer $TOKEN' '$API/servers/$ID' >> '$OUT/killer.log' 2>&1; echo local-killed-$ID >> '$OUT/killer.log'" >/dev/null 2>&1 &
disown 2>/dev/null || true

# 4) wait for ssh
ssh-keygen -R "$IP" >/dev/null 2>&1
up=0; for n in $(seq 1 40); do ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null
  ssh -o ConnectTimeout=5 -o BatchMode=yes root@"$IP" true 2>/dev/null && { say "ssh up"; up=1; break; }
  sleep 10; done
[ "$up" = 1 ] || { echo "FATAL ssh (box $ID left with self-destruct)"; exit 2; }

# 5) ON-BOX self-destruct (primary; survives local machine dying)
ssh root@"$IP" "nohup bash -c 'sleep $((TTL_MIN*60)); curl -s -X DELETE -H \"Authorization: Bearer $TOKEN\" $API/servers/$ID' >/dev/null 2>&1 &" < /dev/null
say "on-box self-destruct armed (${TTL_MIN}m)"

# 6) upload code bundle + sandbox override
scp -q /tmp/rv_code.tgz root@"$IP":/tmp/rv_code.tgz
scp -q _s99val_masks/override.tif root@"$IP":/root/minecraft-worldgen/masks/override.tif
scp -q _render_opt.py root@"$IP":/root/minecraft-worldgen/_render_opt.py
ssh root@"$IP" "cd /root/minecraft-worldgen && tar xzf /tmp/rv_code.tgz && echo extracted" < /dev/null | tee -a "$OUT/box.log"

# 7) render the 4 options in parallel (48 vCPU)
JOB="source /root/venv/bin/activate; cd /root/minecraft-worldgen; export PYTHONUNBUFFERED=1 VANDIR_MASKS_DIR=masks OMP_NUM_THREADS=2; rm -rf opt /root/optdone; mkdir -p opt; "
for spec in "${OPTS[@]}"; do amp="${spec%%:*}"; k="${spec##*:}"
  JOB+="VANDIR_SARID_BUSH_AMP=$amp python _render_opt.py $k opt > /root/o_$k.log 2>&1 & "
done
JOB+="wait; touch /root/optdone"
ssh root@"$IP" "tmux kill-session -t opts 2>/dev/null; tmux new -d -s opts '$JOB'" < /dev/null
say "dispatched 4 options (AMP 2.0/1.3/0.9/0.6 -> tiles 35/36/37/38 @ z79)"

# 8) poll (deadline ~40 min, well under TTL)
for n in $(seq 1 80); do
  d=$(ssh -o ConnectTimeout=8 root@"$IP" "test -f /root/optdone && echo Y || echo N" 2>/dev/null)
  nn=$(ssh -o ConnectTimeout=8 root@"$IP" "ls /root/minecraft-worldgen/opt/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
  say "poll $n: done=$d mca=${nn:-?}/4"
  [ "$d" = "Y" ] && break; sleep 30
done

# 9) collect + install
scp -q root@"$IP":/root/minecraft-worldgen/opt/r.*.mca "$OUT/" 2>/dev/null
ssh root@"$IP" "for f in /root/o_*.log; do echo \"--- \$f ---\"; tail -3 \"\$f\"; done" > "$OUT/render.log" 2>&1 < /dev/null
got=$(ls "$OUT"/r.*.mca 2>/dev/null | wc -l)
say "collected $got/4 mca"
if [ "$got" -ge 1 ]; then cp -f "$OUT"/r.*.mca "$DEST/" && say "installed to Vandirtest10"; fi
ls "$OUT"/r.*.mca 2>/dev/null
say "DONE. box $ID ($IP) self-destructs at ${TTL_MIN}m. Manual kill: curl -s -X DELETE -H 'Authorization: Bearer \$(cat $TOKEN_FILE)' $API/servers/$ID"

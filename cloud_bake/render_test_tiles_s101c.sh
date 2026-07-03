#!/usr/bin/env bash
# render_test_tiles_s101c.sh — provision ONE ccx63, render the 8 S101c realism
# test tiles (4 fix-headlining 2x1 pairs) with the new override/lithology +
# terrain-derived clearing_mask uploaded, collect + auto-delete + verify 0.
# Bias = speed on one box (8 forest tiles ~ a few min post-speedup).
set -u
ROOT="C:/Users/nicho/minecraft-worldgen"; cd "$ROOT" || exit 1
TOK=$(cat /c/Users/nicho/.hetzner_token)
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
KEYID=112518810; IMG=396927540; BOXNAME="s101c-test"
BRANCH="master"
TILELIST="75,70;76,70;68,65;68,66;89,51;89,52;57,48;58,48"
COL="$ROOT/cloud_bake/_collect_s101c"; mkdir -p "$COL"
START=$(date +%s); log(){ echo "[$(date +%H:%M:%S)] $*"; }
api(){ curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" "$@"; }
source "$ROOT/islands/box_watchdog.sh"

# require the new mask locally before spending a box
[ -f masks/clearing_mask.tif ] || { log "!! masks/clearing_mask.tif MISSING — build it first"; exit 1; }

SID=""; IP=""
for loc in fsn1 nbg1 hel1 hil; do
  resp=$(api -X POST "$API/servers" -d "$(printf '{"name":"%s","server_type":"ccx63","image":%d,"location":"%s","ssh_keys":[%d],"start_after_create":true}' "$BOXNAME" "$IMG" "$loc" "$KEYID")")
  sid=$(echo "$resp" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('server',{}).get('id',''))" 2>/dev/null)
  [ -n "$sid" ] && { SID=$sid; log "created @$loc id=$sid"; break; }
done
[ -z "$SID" ] && { log "!! create failed"; exit 1; }
for _ in $(seq 1 60); do
  st=$(api "$API/servers/$SID" | "$PY" -c "import sys,json;s=json.load(sys.stdin)['server'];print(s['status'],s['public_net']['ipv4']['ip'])" 2>/dev/null)
  set -- $st; [ "${1:-}" = "running" ] && [ -n "${2:-}" ] && { IP=$2; log "running ip=$IP"; break; }; sleep 6
done
[ -z "$IP" ] && { log "!! no ip; deleting"; api -X DELETE "$API/servers/$SID">/dev/null; exit 1; }
for _ in $(seq 1 40); do ssh $SSHO root@"$IP" true 2>/dev/null && break; sleep 6; done
box_arm_selfdestruct "$IP" 90

log "git reset -> $BRANCH"
ssh $SSHO root@"$IP" "cd /root/minecraft-worldgen && git fetch origin && git reset --hard origin/$BRANCH && git log --oneline -1"
log "uploading override + lithology + clearing_mask (snapshot masks are stale/absent)"
scp $SSHO -q masks/override.tif        root@"$IP":/root/minecraft-worldgen/masks/override.tif
scp $SSHO -q masks/lithology.tif       root@"$IP":/root/minecraft-worldgen/masks/lithology.tif
scp $SSHO -q masks/lithology_region.png root@"$IP":/root/minecraft-worldgen/masks/lithology_region.png
scp $SSHO -q masks/clearing_mask.tif   root@"$IP":/root/minecraft-worldgen/masks/clearing_mask.tif

# Write the job to a remote FILE via heredoc (unquoted EOF -> $TILELIST expands
# HERE into a properly single-quoted --tile-list on one line). Avoids the
# nested-single-quote + semicolon word-split that only rendered tile 1 last run.
# `rm -rf output` clears stale snapshot output so the collect tar is clean.
ssh $SSHO root@"$IP" "cat > /root/job.sh" <<EOF
source /root/venv/bin/activate
export PYTHONUNBUFFERED=1
cd /root/minecraft-worldgen
rm -rf output /root/done /root/job.log
rm -f masks/_bed_cache_v17.pkl masks/_bed_cache_v19.pkl
python run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-list '$TILELIST' --threads 8 >> /root/job.log 2>&1
touch /root/done
EOF
ssh $SSHO root@"$IP" "tmux kill-session -t tt 2>/dev/null; tmux new -d -s tt 'bash /root/job.sh'" < /dev/null
log "dispatched 8 tiles (job file)"

DEADLINE=$((START+3600))
while :; do
  [ $(date +%s) -gt $DEADLINE ] && { log "!! 60m cap"; break; }
  case "$(box_state "$IP" 900)" in
    DONE) log "done flag seen"; break;;
    FAIL) log "!! FAIL"; ssh $SSHO root@"$IP" "tail -40 /root/job.log" > "$COL/FAIL.log" 2>/dev/null; break;;
  esac
  sleep 20
done
log "collecting"
ssh $SSHO root@"$IP" "cd /root/minecraft-worldgen && tar czf /tmp/tt.tgz output 2>/dev/null"
scp $SSHO -q root@"$IP":/tmp/tt.tgz "$COL/tt.tgz" 2>/dev/null && log "collected $(du -h "$COL/tt.tgz"|cut -f1)" || { log "!! collect failed"; ssh $SSHO root@"$IP" "tail -40 /root/job.log" > "$COL/FAIL.log" 2>/dev/null; }
api -X DELETE "$API/servers/$SID" >/dev/null 2>&1; log "DELETED id=$SID"
rem=$(api "$API/servers" | "$PY" -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin).get('servers',[]) if s['name'].startswith('$BOXNAME')]" 2>/dev/null)
for id in $rem; do api -X DELETE "$API/servers/$id">/dev/null 2>&1; log "swept $id"; done
log "verify: $(api "$API/servers" | "$PY" -c "import sys,json;print(len(json.load(sys.stdin).get('servers',[])),'servers remain')")"
log "DONE ${COL}/tt.tgz elapsed $(( ($(date +%s)-START)/60 ))m"

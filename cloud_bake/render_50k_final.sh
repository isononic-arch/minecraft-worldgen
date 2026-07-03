#!/usr/bin/env bash
# render_50k_final.sh — SAFE full 9,409-tile (97×97) world render across N boxes.
#
# Supersedes the UNSAFE cloud_bake/render_50k.sh (S94e audit): that script
#   (1) scp-uploaded the STALE LOCAL masks/override.tif over the snapshot's
#       correct S87 BT-banding  -> wrong biomes world-wide,
#   (2) skipped tools/heal_height_seams.py  -> ~6-row terrain cliff on every
#       z=512k boundary (the "straggler seam lines"),
#   (3) skipped the bed-cache force-regen  -> stale bed -> dry/staircased rivers.
#
# This script uses the VALIDATED S93 input handling (snapshot override left
# untouched; heal in place; force bed-cache regen; single-threaded PRE-WARM so
# the 40-worker rows HIT a built cache instead of racing the v17->v19 migration)
# + the render_50k round-robin row distribution. Self-provisions from the s93
# snapshot. Renders committed code at origin/<BRANCH> (HEAD must be pushed).
#
# Usage:  bash cloud_bake/render_50k_final.sh
# Env:
#   NBOXES=8        ccx63 boxes (each renders ~12 of the 97 z-rows)
#   THREADS=40      ProcessPoolExecutor workers/box (ccx63 = 48 vCPU / 192 GB)
#   TTL_MIN=300     auto-killer minutes — MUST outlive the ~3.5-4h render
#   SNAPSHOT_ID=396927540   vandir-baked-s93 (S89 rock masks + v19 bed + S87 override)
#   DEST=<world>/region     if set, MCAs copied there after collect
#   KEEP_ALIVE=1   leave boxes up after collect (default: leave up; auto-killer is backstop)
#
# NOTE (S101): this DOES now upload override/lithology/lithology_region per box
# (the promoted S100 override + baby-continent repaint post-date the snapshot —
# see the per-box scp below), and passes --skip-list to exclude the 206
# island-owned ocean regions. It still does NOT flip flags or rebuild derived
# masks — the snapshot ships rock/snow masks and rock_layers/snow_physics are
# committed ON. If you change SNOW config or other derived masks, re-bake the
# snapshot FIRST (memory/override_tif_stale_vs_render.md).
set -u
TOKEN_FILE="/c/Users/nicho/.hetzner_token"
SNAPSHOT_ID="${SNAPSHOT_ID:-396927540}"; BRANCH="master"
NBOXES="${NBOXES:-8}"; GRID=97
THREADS="${THREADS:-40}"; OMP="${OMP:-1}"
TTL_MIN="${TTL_MIN:-300}"
LOC="${LOC:-fsn1}"; OUT_ROOT="${OUT_ROOT:-/d/render_50k_final}"; DEST="${DEST:-}"
API="https://api.hetzner.cloud/v1"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
START=$(date +%s); log(){ echo "[T+$(( ($(date +%s)-START)/60 ))m] $*"; }
TOKEN=$(cat "$TOKEN_FILE"); hz(){ curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }
rm -rf "$OUT_ROOT"; mkdir -p "$OUT_ROOT"
KEY_IDS=$(hz "$API/ssh_keys" | "$PY" -c "import json,sys;print(','.join(str(k['id']) for k in json.load(sys.stdin)['ssh_keys']))")

# round-robin z-rows -> per-box row list (box b gets rows b, b+NB, b+2NB, ...)
declare -a BOX_ROWS; for ((b=0;b<NBOXES;b++)); do BOX_ROWS[$b]=""; done
for ((z=0;z<GRID;z++)); do b=$((z % NBOXES)); BOX_ROWS[$b]="${BOX_ROWS[$b]} $z"; done

# provision
declare -A IP ID
for ((i=0;i<NBOXES;i++)); do
  resp=$(hz -X POST "$API/servers" -d "{\"name\":\"vandir-50k-$i\",\"server_type\":\"ccx63\",\"image\":$SNAPSHOT_ID,\"location\":\"$LOC\",\"ssh_keys\":[$KEY_IDS],\"start_after_create\":true}")
  ID[$i]=$(echo "$resp"|"$PY" -c "import json,sys;print(json.load(sys.stdin).get('server',{}).get('id',''))")
  IP[$i]=$(echo "$resp"|"$PY" -c "import json,sys;print(json.load(sys.stdin).get('server',{}).get('public_net',{}).get('ipv4',{}).get('ip',''))")
  [ -n "${ID[$i]}" ] && [ -n "${IP[$i]}" ] || { echo "FATAL create box$i: $resp"; exit 2; }
  nohup bash -c "sleep $((TTL_MIN*60)); curl -s -X DELETE -H 'Authorization: Bearer $TOKEN' '$API/servers/${ID[$i]}' >> '$OUT_ROOT/autokiller.log' 2>&1; echo killed-${ID[$i]} >> '$OUT_ROOT/autokiller.log'" >/dev/null 2>&1 &
  log "created vandir-50k-$i id=${ID[$i]} ip=${IP[$i]} (auto-killer ${TTL_MIN}m)"; sleep 3
done

# dispatch: git reset -> heal -> rm bed cache -> single-thread pre-warm -> render rows
for ((i=0;i<NBOXES;i++)); do
  ip="${IP[$i]}"; rows="${BOX_ROWS[$i]}"; R0=$(echo $rows | awk '{print $1}')
  ssh-keygen -R "$ip" >/dev/null 2>&1
  for n in $(seq 1 40); do ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null
    ssh -o ConnectTimeout=5 -o BatchMode=yes root@"$ip" true 2>/dev/null && { log "box$i ssh up"; break; }
    [ "$n" = 40 ] && { echo "FATAL box$i ssh"; exit 2; }; sleep 10; done
  ssh root@"$ip" "cd /root/minecraft-worldgen && git fetch origin && git reset --hard origin/$BRANCH && git log --oneline -1" 2>&1 | tee -a "$OUT_ROOT/box$i.boxlog"
  # S101: the snapshot's masks are STALE vs the promoted S100 override (S87
  # banding + S99 baby-continent repaint) — upload the 3 changed masks per box
  # (mirrors render_single_tile.sh STEP 4; option B from the S99 handoff, no
  # snapshot re-bake needed). Everything else still provisions from snapshot.
  log "box$i: uploading override + lithology masks (snapshot copies are stale)"
  scp -q masks/override.tif        root@"$ip":/root/minecraft-worldgen/masks/override.tif
  scp -q masks/lithology.tif       root@"$ip":/root/minecraft-worldgen/masks/lithology.tif
  scp -q masks/lithology_region.png root@"$ip":/root/minecraft-worldgen/masks/lithology_region.png
  RP="python run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/"
  ROWCMDS=""
  # S101: --skip-list excludes the 206 island-owned ocean regions (islands
  # replace them at install; see islands/region_ownership_s101.json). The
  # PRE-WARM below deliberately does NOT get the flag — if tile (0,R0) were
  # skipped the bed cache would never build and 40 workers would race the
  # v17->v19 migration (the exact S93 failure the pre-warm exists to prevent).
  for z in $rows; do z1=$((z+1))
    ROWCMDS+="echo ROW_${z}_START >> /root/job.log; $RP --tile-x0 0 --tile-x1 $GRID --tile-z0 $z --tile-z1 $z1 --skip-list cloud_bake/mainland_skip_regions_s101.txt --threads $THREADS >> /root/job.log 2>&1; "
  done
  JOB="source /root/venv/bin/activate; export PYTHONUNBUFFERED=1 OMP_NUM_THREADS=$OMP OPENBLAS_NUM_THREADS=$OMP MKL_NUM_THREADS=$OMP; cd /root/minecraft-worldgen; rm -f /root/done /root/job.log; "
  JOB+="echo HEAL_START >> /root/job.log; python tools/heal_height_seams.py --inplace >> /root/job.log 2>&1; "
  JOB+="echo REGEN_START >> /root/job.log; rm -f masks/_bed_cache_v17.pkl masks/_bed_cache_v19.pkl; "
  JOB+="echo PREWARM_START >> /root/job.log; $RP --tile-x0 0 --tile-x1 1 --tile-z0 $R0 --tile-z1 $((R0+1)) --threads 1 >> /root/job.log 2>&1; "
  JOB+="echo RENDER_START >> /root/job.log; $ROWCMDS"
  JOB+="python tools/verify_render_health.py --out-dir output > /root/health.txt 2>&1; (cd output && md5sum *.mca > /root/md5.txt 2>/dev/null); touch /root/done"
  ssh root@"$ip" "tmux kill-session -t r50 2>/dev/null; tmux new -d -s r50 '$JOB'" < /dev/null
  log "box$i dispatched: $(echo $rows|wc -w) rows (prewarm tile 0,$R0)"
done

# monitor (deadline TTL+30); print per-box MCA counts
DEADLINE=$(( START + (TTL_MIN+30)*60 ))
declare -A DONE; for ((b=0;b<NBOXES;b++)); do DONE[$b]=0; done
while true; do
  [ "$(date +%s)" -gt "$DEADLINE" ] && { log "POLL TIMEOUT"; break; }
  all=1; st=""
  for ((b=0;b<NBOXES;b++)); do
    [ "${DONE[$b]}" = "1" ] && { st="$st b$b=DONE"; continue; }
    d=$(ssh -o ConnectTimeout=8 root@"${IP[$b]}" "test -f /root/done && echo Y || echo N" 2>/dev/null)
    n=$(ssh -o ConnectTimeout=8 root@"${IP[$b]}" "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l" 2>/dev/null)
    if [ "$d" = "Y" ]; then DONE[$b]=1; st="$st b$b=DONE(${n:-?})"; else all=0; st="$st b$b=${n:-0}"; fi
  done
  log "$st"; [ "$all" = "1" ] && break; sleep 60
done

# collect (boxes kept alive; auto-killer is the backstop — re-render stragglers before kill)
for ((b=0;b<NBOXES;b++)); do
  mkdir -p "$OUT_ROOT/box$b"
  scp -q root@"${IP[$b]}":/root/minecraft-worldgen/output/r.*.mca "$OUT_ROOT/box$b/" 2>/dev/null
  ssh root@"${IP[$b]}" "cat /root/health.txt" > "$OUT_ROOT/box$b.health" 2>/dev/null
  ssh root@"${IP[$b]}" "grep -aE 'HEAL_|REGEN_|PREWARM_|RENDER_|ROW_|MIGRATED|HIT|rebuild|MISMATCH|Done:|error|Traceback' /root/job.log" > "$OUT_ROOT/box$b.jobtail" 2>/dev/null
done
N=$(ls "$OUT_ROOT"/box*/r.*.mca 2>/dev/null | wc -l)
log "collected $N MCAs (expect 9409). health: $OUT_ROOT/box*.health"
if [ -n "$DEST" ] && [ -d "$DEST" ]; then cp -f "$OUT_ROOT"/box*/r.*.mca "$DEST/" && log "installed to $DEST"; fi
for ((b=0;b<NBOXES;b++)); do echo "${ID[$b]} ${IP[$b]}"; done > "$OUT_ROOT/live_boxes.txt"
log "BOXES ALIVE (auto-killer ${TTL_MIN}m). Kill when satisfied: while read id ip; do curl -s -X DELETE -H \"Authorization: Bearer \$(cat $TOKEN_FILE)\" $API/servers/\$id; done < $OUT_ROOT/live_boxes.txt"

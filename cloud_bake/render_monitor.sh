#!/usr/bin/env bash
# render_monitor.sh — LIVE render-failure monitor for render_verify.sh.
#
# Watches the SAME curated tile list + box round-robin that render_verify.sh
# uses, probes each box read-only over ssh, and classifies every tile so no
# silently-missing / errored / hung / empty tile ends up in the world.
#
# Per-tile states:
#   PENDING   no per-tile log yet (box still in mask-build, or not reached)
#   BUILDING  box's rv_build.log has no BUILD_DONE yet
#   RUNNING   log exists, no completion marker, recently active
#   HANG      log exists, no completion, silent > HANG_MIN minutes  <-- FLAG
#   ERROR     log shows Traceback/MemoryError/malloc/Killed/Error   <-- FLAG
#   EMPTY     completed but output MCA < MIN_MCA_BYTES (0 chunks)    <-- FLAG
#   DONE      completed + MCA looks populated
#
# Loops until every box touches /root/rv_done (or Ctrl-C), then prints a final
# reconciliation: expected vs DONE vs FLAGGED vs MISSING. Exit 0 only if every
# expected tile is DONE. Read-only: never writes to the boxes.
#
# Usage (mirror render_verify.sh's TILES + IPs):
#   TILES=memory/verify_tiles.txt bash cloud_bake/render_monitor.sh IP1 IP2 ... IP8
# Env: INTERVAL (poll secs, default 45), HANG_MIN (default 35),
#      MIN_MCA_BYTES (empty-MCA threshold, default 50000).
set -u
[ "$#" -ge 1 ] || { echo "Usage: $0 IP1 [IP2 ...]"; exit 1; }
IPS=("$@"); NB=${#IPS[@]}
TILES="${TILES:-memory/verify_tiles.txt}"
INTERVAL="${INTERVAL:-45}"
HANG_MIN="${HANG_MIN:-35}"
MIN_MCA_BYTES="${MIN_MCA_BYTES:-50000}"
START=$(date +%s)
log(){ echo "[T+$(( ($(date +%s)-START)/60 ))m] $*"; }

[ -f "$TILES" ] || { echo "TILES not found: $TILES"; exit 1; }
mapfile -t TLINES < "$TILES"
TLINES=("${TLINES[@]%$'\r'}")   # strip CR (Windows line endings)

# Replicate render_verify.sh round-robin EXACTLY (skip blank lines, in order).
declare -a BOX_TILES; for b in "${!IPS[@]}"; do BOX_TILES[$b]=""; done
declare -A TILE_BOX
i=0; EXPECTED=0
for line in "${TLINES[@]}"; do
  [ -z "${line// }" ] && continue
  b=$(( i % NB )); BOX_TILES[$b]="${BOX_TILES[$b]}|$line"
  tx=${line%% *} tz=${line##* }; TILE_BOX["$tx,$tz"]=$b
  i=$((i+1)); EXPECTED=$((EXPECTED+1))
done
log "Monitoring $EXPECTED tiles across $NB box(es) from $TILES (hang>${HANG_MIN}m, empty<${MIN_MCA_BYTES}B)"

declare -A STATE   # "tx,tz" -> state string

# One ssh per box per cycle: emit a CSV line per assigned tile.
probe_box(){
  local b="$1" ip="${IPS[$b]}" tiles="${BOX_TILES[$b]}"
  local pairs=""; IFS='|' read -ra TS <<< "$tiles"
  for t in "${TS[@]}"; do [ -z "${t// }" ] && continue; pairs+="${t% *}_${t##* } "; done
  ssh -o ConnectTimeout=10 root@"$ip" "
    cd /root/minecraft-worldgen 2>/dev/null || exit 0
    bd=0; grep -q BUILD_DONE /root/rv_build.log 2>/dev/null && bd=1
    dn=0; test -f /root/rv_done && dn=1
    echo \"BOX,$b,\$bd,\$dn\"
    for p in $pairs; do
      tx=\${p%_*}; tz=\${p#*_}
      L=/root/rv_\${tx}_\${tz}.log; ex=0; comp=0; err=0; mt=0; sz=0
      if [ -f \"\$L\" ]; then ex=1
        grep -qE 'pipeline_complete|tile_complete|\"type\": \"tile_complete\"|^Done:' \"\$L\" && comp=1
        grep -qiE 'Traceback|MemoryError|malloc|Killed|Cannot allocate|Out of memory|tile_error|Error:' \"\$L\" && err=1
        mt=\$(stat -c %Y \"\$L\" 2>/dev/null || echo 0)
      fi
      M=output/r.\${tx}.\${tz}.mca; [ -f \"\$M\" ] && sz=\$(stat -c %s \"\$M\" 2>/dev/null || echo 0)
      echo \"T,\$tx,\$tz,\$ex,\$comp,\$err,\$mt,\$sz\"
    done
  " 2>/dev/null
}

classify(){ # ex comp err mt sz builddone  -> state
  local ex=$1 comp=$2 err=$3 mt=$4 sz=$5 bd=$6 now; now=$(date +%s)
  if [ "$err" = 1 ]; then echo ERROR; return; fi
  if [ "$comp" = 1 ]; then
    if [ "$sz" -lt "$MIN_MCA_BYTES" ]; then echo EMPTY; else echo DONE; fi; return
  fi
  if [ "$ex" = 1 ]; then
    if [ $(( (now - mt) / 60 )) -ge "$HANG_MIN" ]; then echo HANG; else echo RUNNING; fi; return
  fi
  [ "$bd" = 1 ] && echo PENDING || echo BUILDING
}

declare -A BOX_DONE; for b in "${!IPS[@]}"; do BOX_DONE[$b]=0; done
trap 'echo; log "interrupted — printing summary"; summary; exit 130' INT

summary(){
  local ok=0 flagged=0 missing=0; local -a FLAGS=() MISS=()
  for key in "${!TILE_BOX[@]}"; do
    local s="${STATE[$key]:-MISSING}"
    case "$s" in
      DONE) ok=$((ok+1)) ;;
      ERROR|HANG|EMPTY) flagged=$((flagged+1)); FLAGS+=("$key=$s") ;;
      *) missing=$((missing+1)); MISS+=("$key=$s") ;;
    esac
  done
  echo "================ RENDER MONITOR SUMMARY ================"
  echo "expected=$EXPECTED  DONE=$ok  FLAGGED=$flagged  not-finished=$missing"
  [ "${#FLAGS[@]}" -gt 0 ] && { echo "FLAGGED (errored/hung/empty):"; printf '  %s\n' "${FLAGS[@]}" | sort; }
  [ "${#MISS[@]}" -gt 0 ]  && { echo "NOT FINISHED:"; printf '  %s\n' "${MISS[@]}" | sort; }
  [ "$ok" = "$EXPECTED" ] && echo "ALL $EXPECTED TILES OK — safe to install." || echo "DO NOT install until flagged/unfinished tiles resolve."
  echo "======================================================="
}

while true; do
  all_done=1
  # gather all boxes
  for b in "${!IPS[@]}"; do
    box_bd=0
    while IFS=',' read -r tag f1 f2 f3 f4 f5 f6 f7; do
      if [ "$tag" = "BOX" ]; then
        box_bd=$f2; [ "$f3" = 1 ] && BOX_DONE[$f1]=1
      elif [ "$tag" = "T" ]; then
        # T,tx,tz,ex,comp,err,mt,sz
        STATE["$f1,$f2"]=$(classify "$f3" "$f4" "$f5" "$f6" "$f7" "$box_bd")
      fi
    done < <(probe_box "$b")
  done
  # Build live status counts
  declare -A CNT=(); for key in "${!TILE_BOX[@]}"; do s="${STATE[$key]:-PENDING}"; CNT[$s]=$(( ${CNT[$s]:-0} + 1 )); done
  line=""; for s in DONE RUNNING BUILDING PENDING HANG ERROR EMPTY; do [ -n "${CNT[$s]:-}" ] && line="$line $s=${CNT[$s]}"; done
  log "$line"
  for b in "${!IPS[@]}"; do [ "${BOX_DONE[$b]}" = 0 ] && all_done=0; done
  [ "$all_done" = 1 ] && break
  sleep "$INTERVAL"
done
log "all boxes report rv_done"
summary
[ "$(for k in "${!TILE_BOX[@]}"; do echo "${STATE[$k]:-MISSING}"; done | grep -vc '^DONE$')" = 0 ]

#!/usr/bin/env bash
# box_watchdog.sh — sourceable helpers that make the cloud poll-loop FAIL FAST
# instead of idling for the 3h backstop when a box crashes or stalls.
#
# WHY (S96): the poll loop only checked `/root/all_done`. When a box died on
# tile 1 (e.g. the tarball-dropped-DEM FileNotFoundError), `all_done` never
# appeared, so the box billed a full idle hour until the hard cap. These helpers
# tail `/root/run.log` for crash signatures and detect a stalled log (no new
# output for N seconds) so the orchestrator can reap the box immediately.
#
# Usage in a cloud script:
#   source islands/box_watchdog.sh
#   ...inside the per-box poll body, BEFORE the all_done test:
#   case "$(box_state "$ip")" in
#     FAIL) ssh $SSHO root@"$ip" "cat /root/run.log" > "$COL/${NAME[$i]}.FAIL.log"
#           del_box "$i"; DONE[$i]=1; continue;;
#     STALL) log "  !! ${NAME[$i]} STALLED (no log progress)"; ... ;;
#   esac
#
# Also: arm an on-box self-destruct so a box can NEVER outlive the budget even if
# the orchestrator (your laptop) dies mid-poll:  box_arm_selfdestruct "$ip" 180
SSHO_DEFAULT="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15"

# Track last-seen log size + timestamp per IP in temp files (keyed by IP).
_wd_state_dir="${TMPDIR:-/tmp}/box_wd"; mkdir -p "$_wd_state_dir"

# box_state <ip> [stall_secs]  ->  prints one of: FAIL | STALL | DONE | RUN | UNREACHABLE
box_state() {
  local ip="$1" stall="${2:-600}" ssho="${SSHO:-$SSHO_DEFAULT}"
  local tail size now key last_size last_ts
  key="$_wd_state_dir/$(echo "$ip" | tr '.' '_')"
  # one ssh round-trip: done-flag, log size, and the crash-relevant tail
  local probe
  probe="$(ssh $ssho root@"$ip" '
     test -f /root/all_done && echo "__DONE__"
     echo "__SIZE__$(wc -c < /root/run.log 2>/dev/null || echo 0)"
     tail -c 4000 /root/run.log 2>/dev/null' 2>/dev/null)" || { echo UNREACHABLE; return; }
  case "$probe" in *__DONE__*) echo DONE; return;; esac
  case "$probe" in
    *Traceback*|*FileNotFoundError*|*"MemoryError"*|*"Killed"*|*"No space left"*|*"OOM"*|*"CUDA error"*)
        echo FAIL; return;;
  esac
  size="$(printf '%s\n' "$probe" | sed -n 's/.*__SIZE__\([0-9]*\).*/\1/p' | head -1)"
  now="$(date +%s)"
  if [ -f "$key" ]; then read -r last_size last_ts < "$key"; else last_size=-1; last_ts=$now; fi
  if [ "$size" = "$last_size" ]; then
    [ $((now - last_ts)) -ge "$stall" ] && { echo STALL; return; }
  else
    echo "$size $now" > "$key"
  fi
  echo RUN
}

# box_arm_selfdestruct <ip> <minutes> — schedule an unconditional poweroff on the
# box so a leaked box can't bill past the budget if the orchestrator dies. Uses a
# detached nohup sleep (no `at`/cron dependency on the minimal image).
box_arm_selfdestruct() {
  local ip="$1" mins="${2:-180}" ssho="${SSHO:-$SSHO_DEFAULT}"
  ssh $ssho root@"$ip" "nohup sh -c 'sleep $((mins*60)); poweroff -f' >/dev/null 2>&1 &" 2>/dev/null
  echo "  armed self-destruct on $ip (+${mins}m)"
}

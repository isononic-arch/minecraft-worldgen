#!/usr/bin/env bash
# _box_bake_all_s101.sh — ON-BOX helper: bake ALL 15 islands' masks CONCURRENTLY
# (bakes are single-threaded numpy; a ccx63 has 48 vCPU / 192GB so wall time =
# slowest single island). Writes /root/all_done with per-island OK/FAIL lines,
# tars islands/masks_islands for collection. Dispatched by _cloud_bake_masks_s101.sh.
set -u
cd /root/minecraft-worldgen
PY=/root/venv/bin/python
NAMES="new_vincentia kostati margarita bahamas anguilla la_tortuga los_roques grenada grand_turk admiralty ouvea loyalty efate fogo madre"

rm -rf islands/masks_islands
mkdir -p /root/bakelogs
pids=""
for n in $NAMES; do
  ( PYTHONUNBUFFERED=1 "$PY" islands/render_islands.py --bake "$n" \
      > "/root/bakelogs/$n.log" 2>&1 \
      && echo "OK $n" >> /root/bake_results \
      || echo "FAIL $n" >> /root/bake_results ) &
  pids="$pids $!"
done
wait $pids

ok=$(grep -c '^OK ' /root/bake_results 2>/dev/null || echo 0)
fail=$(grep -c '^FAIL ' /root/bake_results 2>/dev/null || echo 0)
tar czf /tmp/masks.tgz islands/masks_islands
{ echo "baked ok=$ok fail=$fail"; cat /root/bake_results; } > /root/all_done

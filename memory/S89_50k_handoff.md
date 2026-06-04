# S89 → 50k REGEN handoff (live state, 2026-06-04)

## WHERE WE ARE RIGHT NOW
The whole S89 surface overhaul is **validated and done**. Full 32-tile biome
sweep rendered at commit `7443491` + final tweaks at `51d41f4` (steppe podzol→
grass, coastal-heath sand→white-noise speckle). User walked the world: **all
biomes good.** Only remaining step = the **full 50k world regen**.

## IMMEDIATE: calibration in flight
- **Calibration running on box `78.47.145.92`** (commit `51d41f4`), tmux session
  `cal`, output `/root/cal_out/`. Rendering z=50 full row (97 tiles, ~half land)
  at `THREADS=40 OMP=1` to measure tiles/min. Records `/root/cal_b0..b1` (mask
  build secs), `/root/cal_r0..r1` (render secs), `/root/cal_done`.
- A background monitor was polling it (task id was `be6q150j5`). If lost to
  compaction, re-poll: `ssh root@78.47.145.92 "cat /root/cal_b0 /root/cal_b1 /root/cal_r0 /root/cal_r1; ls /root/cal_out/r.*.mca|wc -l"` and compute
  rate = tiles/((r1-r0)/60).
- **Use the measured rate to lock THREADS + total ETA**, then fire the full run.

## THE FIRE PROCEDURE (8 boxes, user max = 8)
1. User provides: **8 box IPs** + **external SSD mount path** (e.g. `/d/...` or
   `/e/...` in Git-Bash). MCAs (~50-100 GB, 9409 region files) go to the SSD —
   NOT Vandirtest10 (that's the test world). Spline was redone so **NO ocean
   skip** — render all 9,409.
2. Script: `cloud_bake/render_50k.sh` — round-robin z-rows (box b: rows b,b+8,..),
   builds rock_layers/talus/cap/snow_physics on each box (~5 min, one-time),
   then run_pipeline per row at `--threads $THREADS`, OMP=$OMP. Collects to
   `OUT_DIR` (currently hardcoded `output_50k` — **TODO: make OUT_DIR an env
   override OR edit it to the SSD path before firing**). `DEST=<world>/region`
   auto-installs if set.
   Launch: `DEST=... THREADS=40 OMP=1 bash cloud_bake/render_50k.sh IP1..IP8`
   Run with run_in_background=true, NO trailing `&` (that detaches+kills the
   collect loop — caused a walk-4 mishap).
3. Monitor: `cloud_bake/monitor.ps1 -Ips ip1,ip2,...` in a PowerShell window —
   live per-box counts, ETA (tiles/min), 25/50/75% beeps, 40-min stall alert,
   completion fanfare. Auto-computes 1261/1164×7 quotas for 8 boxes.

## ESTIMATE (un-piloted; calibration will firm it)
- 9,409 tiles, ~30% land. Per-box ~24-40 concurrent (CCX63 48 vCPU / 184 GB —
  memory NOT a constraint, cores are; OMP=1 + high threads = max parallelism).
- Rough: ~3-5 h wall on 8 boxes, ~€15-30. Optimizations applied: THREADS 40 /
  OMP 1. More boxes would help but user capped at 8. Ocean-skip ruled out.

## COMMIT STATE
- Branch `s85-cherry-picks`. `51d41f4` PUSHED (boxes pull this = pipeline code).
- `render_50k.sh` + `monitor.ps1` committed locally (orchestration; don't need
  pushing for boxes, but push for tidiness).

## GOTCHAS (hard-won this session)
- After dispatch, ALWAYS verify boxes pulled the expected commit (grep dispatch
  log for the hash) before trusting the render.
- collect/wait loops: until-condition must NOT use `exit 1` (kills script). Use a
  brace-group ending in a test, e.g. `until { ...; [ $n -eq 8 ]; }; do sleep; done`.
- Fully quit+reopen MC before walking (stale client chunks).
- vandir_height.zip datapack mandatory in any world before first load.

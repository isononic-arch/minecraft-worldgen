# S94 RIVER WATER SEAM/SPILL HANDOFF (read before touching river water)

Long saga fixing river water across tile seams. **Branch `s85-cherry-picks`.**
The goal: water that is (a) **seam-clean** (no 1-block waterline at tile borders),
(b) **contained** (never sitting above the surrounding terrain = no spill/perch),
(c) keeps the water (don't drain the trough), (d) no walls/air, (e) full natural
paint. These can't ALL be had cleanly at the tile level — see why below.

## CURRENT STATE (what to ship)
- Code reverted to the **seam-clean GLOBAL OVERRIDE** (commit `9f006a0`):
  `rebuild_river_wl.py` = band-based full-coverage bake -> `masks/hydro_river_wl.tif`
  (int16 water level, -999 off-river); `run_pipeline.py` Step-9 OVERRIDEs the
  per-tile flood-settle water with the global mask on `_riv_w` cells. This is
  SEAM-CLEAN but OVER-LEVELS some tiles (water above terrain).
- **+ spill-TRIM pass** `core/water_cleanup.py:cleanup_spill_rows` (called in
  run_pipeline after the bank-taper, before crop, on the PADDED arrays). It does
  what the user actually asked: a surface-water cell that sits above its lowest
  surrounding wall (min of 4-neighbour tops: water level if water else terrain
  top) is LOWERED to that wall (never below its bed), iterated so the drop
  propagates inward across a flooded sheet. => removes the EXPOSED TOP LAYER(S),
  keeps the water in the trough. NOT delete-the-column (that drained 13,80 dry —
  the user's "what the fuck" screenshot), NOT raise-land (walls).
- masks/hydro_river_wl.tif = the band-based FULL-COVERAGE bake (7.9MB). Good.
- water_hug DISABLED/removed (caused walls + 1x3 air pockets = exposed wall faces).

## DO-NOT-REPEAT (every approach tried + why it failed)
1. **per-tile flood-settle** (the ORIGINAL approved water, "it's great"): contained
   + natural, but ±1 SEAM on wide rivers (its monotone is per-tile -> diverges at
   the halo). The seam is the whole reason this saga started.
2. **global override** (seam-clean): PERCHES — its level is computed from a
   centerline source that is above the local banks, so water sits 1-2 ABOVE
   terrain (esp. 13,80: above terrain across the WHOLE tile).
3. **source-fold** (contain the global level against the CARVED surface in
   settle): RE-BREAKS the seam (carved surface diverges per-tile at the halo;
   band grouping diverges). seam 1->50.
4. **water-hug** (RAISE land to the water level): WALLS (swimming-pool) + the
   "1x3 air pockets" = the exposed vertical faces of those walls. Can't have a
   concave berm that is both >= water AND flush with lower ground.
5. **fill-to-spill bake** (priority-flood / skimage reconstruction so level never
   exceeds terrain): (a) only fills CLOSED basins — can't reproduce the carver's
   FLOWING wide floods (52,53 covered 3%); (b) skimage reconstruction is far too
   slow at 50k — **it ran 53 min on a box without finishing and I had to kill it
   (~€0.55 wasted). ALWAYS benchmark a 50k-scale algorithm locally before a box.**
   A coarse (1:8) reconstruction + upscale was fast but the coverage/flowing-flood
   problem remained.
6. **delete-whole-row cleanup**: drained the trough DRY (deleted the water column
   down to the bed). WRONG. The user wanted only the exposed TOP layer removed.

## THE CORE TENSION (so you don't re-discover it the hard way)
The global level is seam-clean but over-levels (above terrain) in spots. To fix a
spill at the tile level you must EITHER lower the water (per-tile -> seam) OR raise
land (wall). There is no tile-local way to get seam-clean AND contained when the
level is above the ground. The spill-TRIM accepts a (small, terrain-driven) lower
of the overspilled top — terrain is global so it SHOULD stay ~seam-consistent;
verify the seam after rendering. If a heavily-over-leveled tile (13,80) shows a
re-appearing seam after trim, the real cure is the bake not over-leveling there
(hard: needs the carver's flowing-flood logic globally).

## INSTALLED / VERIFY WORLD
- Verify world: `/d/modrinth_vandir/saves/Vandir50k_verify/region/`. 13,80 was
  installed with the BAD delete-cleanup (mostly dry) — RE-INSTALL with the trim
  version once the render confirms it keeps water.
- Walk tiles: 13,80 perch spot `/tp @s 6703 90 41180`; seam (52,53)|(52,54)
  `/tp @s 26880 90 27648`; (12,80)|(13,80) seam `/tp @s 6656 90 41216`.

## NEXT
1. Confirm `diag_s94_trim/r.13.80.mca` KEEPS water (not dry) + perch ~0. Install it.
2. Box all 4 river-crossing tiles (52,53/52,54/12,80/13,80) via
   `cloud_bake/river_wl_bake.sh` (it rebuilds the band-based mask + renders).
   **NOTE: that script runs `rebuild_river_wl.py --scale 1` = band-based ~3min,
   NOT the slow reconstruction.** Verify perch (tools/diag_mca_surface_perch.py),
   seam (tools/diag_seam_readout.py), AND that water is kept (not drained).
3. If a seam re-appears on trimmed tiles, decide: accept small seam, or the big
   global-carver-flood rewrite.

## COMPUTE DISCIPLINE (user is cost-sensitive + was burned)
- Validate algorithms LOCALLY before any box (the reconstruction box waste).
- `cloud_bake/river_wl_bake.sh` single-box, sequential, foreground poll +
  HARD poll-timeout (TTL+15). Auto-killer armed. See [[box-dispatch-hang]].
- Always confirm 0 servers after: `GET /v1/servers`. Kill stale local bash polls.

# S94 HANDOFF — river overhaul (flood-settle + bank-taper + dirt risers + despike)

**Session S94 (2026-06-13/14), branch `s85-cherry-picks`, on top of `d7a381f`.**
A LARGE river-rendering overhaul was built and walked this session. Most is
USER-WALKED-AND-APPROVED; the final despike is offline-validated, pending the
last walk. **READ THIS BEFORE TOUCHING RIVERS.**

## STATE AT HANDOFF
- `d7a381f` (committed/pushed): schematic load cache (byte-exact gated) + render
  health verifier (`tools/verify_render_health.py`) + minimal 20-tile set
  (`memory/verify_s94_tiles.txt`) + `cloud_bake/render_s94_validation.sh`.
- **The river overhaul below is UNCOMMITTED at handoff** (commit it — see end).
  Files: `run_pipeline.py`, `core/river_flood_settle.py` (new),
  `core/bank_taper.py` (new), `core/chunk_writer.py`, `core/surface_decorator.py`,
  `config/thresholds.json`.
- Also uncommitted: granitic `rock_layers.groups.granitic.rib_mid_contrast`
  `light`->`dark` (user config-only edit, not yet rendered/walked).
- **(84,60) installed to `Vandir50k_verify` for walking** (the dev/iteration tile).
- Hetzner: the S94 20-tile validation render RAN (boxes torn down, 0 remaining,
  $ stopped). Token was rotated mid-session into `C:/Users/nicho/.hetzner_token`
  (may still be present — wipe if leaving). Verify world install of those 20
  tiles is DONE (the stale-sweep tiles got re-rendered as part of it).

## THE RIVER SYSTEM (6 passes, pipeline order) — what each does + where

The whole thing replaces the old per-cell v8.14 water-level cap (which caused a
lengthwise "split down the middle" on wide channels). All gated in
`config.river_carve`. Pipeline order in run_pipeline.py Step-9 (padded block) +
chunk_writer:

1. **flood-settle** (`river_carve.flood_settle.enabled`, method `xsecspill`).
   `core/river_flood_settle.py:settle()`. The MC "glass-platform" trick simulated:
   per true cross-section (nearest-CENTERLINE slice) settle to min(clean carver
   source, lowest lateral bank), flat per latitude band, contained by construction
   (ZERO levees — spill drops it), monotone non-increasing toward ocean. Lumpy bed
   NEVER touched. **CRITICAL: `land=` arg MUST exclude LAKES** (a lake is water,
   not a bank — else a river beside a lake drains to the lake bed; this was a
   real bug that drained (62,61) to -15 depth, fixed). Runs on the PADDED halo
   (seam-safe). Source = `_clean_water_pad_s94` (the carver water snapshot pre-cap,
   = river_water_y at line ~916). Result: flat terraced water, ~11% emergent rocks.
   USER WALKED (84,60): "its great... i like the rocky outcrops."
2. **bank-taper** (`river_carve.bank_taper.enabled`). `core/bank_taper.py:taper()`,
   variant rampgauss. Trough walls -> gentle gaussian valley: 1-cell LAND perimeter
   flush at water W, 2nd ring W+1 (secondary containment wall), then constant-grade
   ramp (GRADE=0.34, reach SCALES WITH WALL HEIGHT) gaussian-smoothed to terrain.
   ONLY lowers; terrace-safe; never touches bed/water/emergent-rock. Runs on padded
   halo after flood-settle (rock re-lock skips river banks so no conflict).
   - **(A) step-limit** (inside taper, `MAX_BANK_STEP=1`, `STEP_MIN_REACH=8`): caps
     bank steps to <=1 block so every riser shows the dirt veneer not stone.
     **GOTCHA (cost a lot of debugging):** must run AFTER the taper's final
     only-lower cap (else it sees the ramp, not the real surface); containment
     floor = ONLY adjacent-wet-W (NOT the broad terr_floor, which blocked it);
     domain = land within max(reach, STEP_MIN_REACH) (flat-shore bumps too, not
     just the wall). It RELOCATES the residual >=2 step to the apron/natural
     boundary; that boundary riser is handled by B below.
   USER: "banks are great."
3. **despike** (`river_carve.bank_taper.despike_rock`, default True).
   `core/bank_taper.py:despike_emergent_rock()`. Lowers THIN (1-wide, no 2-wide
   erosion core) tall emergent-rock columns -- the "stonehenge" pillars the user
   walked -- to blend into surroundings; BROAD outcrops (erosion core survives)
   kept. Only emergent river-footprint cells, only lowers. Offline-validated on
   (84,60): 7 thin cells removed, 0 thin remaining, all 43 broad kept.
   **PENDING FINAL USER WALK.**
4. **rock-poke paint** (Phase 2, gated with flood_settle.paint_rocks default True).
   `core/surface_decorator.py:paint_river_rocks()`, called from run_pipeline after
   the crop. Emergent river-bed cells (river_meta>0 & water>SEA & surface_y>=water)
   painted the per-cell lithology DARK band (`rock_layers.groups[g].dark`). USER
   LIKES the rocky outcrops.
5. **B dirt risers** (in `core/chunk_writer.py`, `_BANK_SOIL_RADIUS=12`,
   `_BANK_SOIL_MAX=6`). ROOT CAUSE of "stone columns": bank cells next to rivers
   have lithology_tile==0 -> `_fill_geology_layers` keeps STONE (no soil horizon).
   Fix: `build_column_array` computes a bank mask (land within 12 of river water)
   and **stamps DIRT directly** from surface_y-1 down each cell's riser height
   (independent of lithology). Verified at block level: all 143 riser faces dirt,
   0 stone. World-wide rock cliffs untouched (radius-limited).
6. (rock paint runs here too — see 4.)

## WHAT'S VALIDATED vs PENDING
- Water (flood-settle): USER WALKED (84,60), approved. Lake fix on (62,61).
- Banks (taper + step-limit + B dirt risers): USER WALKED, "banks are great",
  block-verified dirt risers.
- Despike (stonehenge): OFFLINE-validated only -> **NEEDS FINAL WALK of (84,60).**
- Granitic rib=dark: config-only, NOT rendered/walked.
- Cross-tile SEAMS: taper/settle run on padded halo (seam-safe by design) but a
  real river-crossing seam pair has NOT been rendered+walked. (84,60)|(84,61)
  tested but the river doesn't cross that seam. DO a real seam pair before 50k.

## KNOWN / NEXT
- **THE ORIGINAL SESSION GOALS ARE STILL UNDONE:** (1) the biome-roster walk
  (`memory/BIOME_VALIDATOR_CHECKLIST.md` + `memory/verify_s94_tiles.txt` 20 tiles
  were rendered+installed, but the user has only walked rivers, NOT the biome
  roster); (2) re-sync the validate_3x3 48_48 baseline (validator drift). The
  whole session became the river overhaul.
- Performance: the step-limit loop + despike label-loop + B per-cell dirt loop
  add per-tile cost. Fine for single tiles; PROFILE before the 8-box 50k (user
  cares: ~3.5h on 8 ccx63, costly). The Insights PDF flagged perf wins.
- Before 50k: render a real river-crossing seam pair (banks + water continuity);
  confirm `verify_render_health.py` gates the silent-failure modes; bed-cache law
  (NEVER delete v17; box log must show MIGRATED/HIT).
- Diag tools written this session (keep): `tools/verify_render_health.py`,
  `tools/diag_s94_tileset.py`, `tools/diag_water_*.py`, `tools/diag_river_coord.py`,
  `tools/diag_verify_dirt_risers.py`, `tools/_s94_*` (workflow scratch).

## TUNABLES (config + module constants)
- `river_carve.flood_settle`: {enabled, method:xsecspill, paint_rocks}
- `river_carve.bank_taper`: {enabled, grade:0.34, sigma:2.0, max_reach:48, despike_rock}
- `core/bank_taper.py`: GRADE, SIGMA, MAX_REACH, MAX_BANK_STEP=1, STEP_MIN_REACH=8
- `core/chunk_writer.py`: _BANK_SOIL_RADIUS=12, _BANK_SOIL_MAX=6
- `river_carve.headwater_taper.enabled=False` (S93 parked; leave OFF).

## DEV LOOP USED
Single-tile local render w/ dump: `SURF_DUMP_STEP9_DIR=<dir> py run_pipeline.py
... --tile-x0 84 --tile-x1 85 --tile-z0 60 --tile-z1 61 --threads 1`. Verify
blocks via `tools/diag_verify_dirt_risers.py` (read_chunk/section_block helpers).
Install: cp `<dir>/r.84.60.mca` to
`/d/modrinth_vandir/saves/Vandir50k_verify/region/` (D: is an external drive —
it dropped off once mid-session; if `/d` missing, it's unplugged).

# S89 walk4 — in-world review fixes + lake/river diagnosis (2026-06-06)

User reviewed the 78-tile verify world. Findings + what I did.

## Diagnostic tooling built (self-verify without in-game)
- `tools/diag_mca_water.py` — reads a region .mca, top-down + depth heatmap PNGs,
  splits sea-level (<=Y63) from inland water, depth histogram, `--xsec Zworld`
  cross-section. THIS is how to check lakes/rivers from rendered data.
- `tools/diag_tree_altitude.py` — tree-column density by altitude band. NOTE: trunk
  height is NOT a krummholz discriminator (trees sunk via MAX_TREE_SINK -> only 1-2
  logs above surface in MCA, all trees read ~1.3). Use density% + KR_DEBUG instead.
- `KR_DEBUG=1` env on a render -> `core/schematic_placement.py` prints krummholz-vs-
  regular tree counts by altitude band per tile. Definitive krummholz check.

## #12 dry-lake force-flood — REVERTED (config dry_lake_force_carve_max: 4 -> 0)
A/B proof: original pre-#12 world (D:/modrinth_vandir/saves/Vandir50k/region) vs #12
verify (D:/verify_out) for r.33.33 are BYTE-IDENTICAL in the lake bed striping. So #12
was NOT the cause of the ugly lakes. #12 only added a messy 1-deep fringe (+~80
shallow cols at 33,33) and filled some cells (19,76 +9k). Net negative -> reverted.

## ROOT CAUSE of bad lakes (PRE-EXISTING, not my recent work)
`core/river_carver_v2.py` ~line 441/465: `lake_mask = basin & (terrain_y < water_y)`.
Lakes are TERRAIN-INTERSECTION: water only fills where natural Gaea terrain dips below
the spill elevation. For a flat basin sitting AT its spill level, water pools ONLY in
the deep river channels cut through it (Y63 trenches) with basin floor (Y79) forming
dry walls between -> the "striped / 90-degree / 1-deep" look the user sees.
- 36,33 / 37,34 / 38,35 / 75,48: inland water = 0. These "lakes" are SEA-LEVEL coastal
  water (Y48-63), not inland lakes. "default ocean" is correct.
- 81,64: ZERO water in BOTH original and verify -> never was a lake (not a regression).
- 33,33 / 19,76: genuine deep lakes (depth ~16) BUT chopped into stripes by Y79 walls.
Per CLAUDE.md HARD RULE: "Shoreline = terrain intersection... if a lake is wrong, fix
it in Gaea, not in post." Pipeline hacks have failed repeatedly (v23/v25/v26/#12).

## BOWL-CARVE ATTEMPT (user chose "attempt pipeline bowl-carve") — PARKED, flag OFF
Implemented `core/river_carver_v2.py` `lake_bowl_carve` flag (config
hydrology_engine.river_geometry.lake_bowl_carve, currently FALSE). It floods the whole
precompute basin and carves bed to (water_y - lkdep) using hydro_lkdep — which IS a
real synthetic bowl: hydrology_precompute.py:1192-1203 combined = max(terrain_depth,
(dist_from_shore_norm**1.3)*max_depth*0.6), min 2. So lkdep>0 across ~99.7% of basin.
LOCAL VERIFY (33,33, output_local): the carve carves correctly where DEEP (river rows,
e.g. z=254 walls -> Y63, wet) but the SHALLOW lake-wall carves (2-11 blocks) DON'T
STICK -> still striped, and net water DROPPED 56967 -> 35435 (drains more than fills).
ROOT CAUSE = antipattern #2: carve_rivers (run_pipeline.py:205) is followed by
apply_flow_erosion (241), peak_crunch smoothing (513-551), bed-smooth v16 (1034), bank
smooth (1091) — these REFILL the shallow lake carves while leaving deep river carves.
To make bowl-carve work needs a LAKE-BED RE-LOCK after line ~1091 (analogous to the
_crunch_lock_y rock lock at 1254-1261). BUT lkdep is 1:8 NEAREST-upscaled (8-block
staircase) and bed-smooth-v16 exists to smooth exactly that -> naive re-lock trades
striping for a STAIRCASED bowl. Needs care (smooth lkdep within basin before carve, or
include lake-wall cells in bed-smooth-v16). HIGH RISK area (surface_y ordering).
=> USER DECISION: (a) accept lakes as-is (#12 reverted = previously-accepted state), or
(b) greenlight the lake-bed re-lock + bowl-smoothing work (I verify locally each step),
or (c) Gaea re-sculpt the flat-at-spill basins in the source heightmap (canonical per
hard rule). Bowl-carve CODE is preserved flag-gated for option (b).

## *** LAKE FIX WORKS — bowl-carve + lake-wins + re-lock. flag ON. (S89-walk4 FINAL) ***
The whole "striping / can't fix / go to Gaea" saga was caused by a TRANSPOSE BUG in the
diagnostic tool tools/diag_mca_water.py (and diag_tree_altitude.py), NOT the terrain.
The bit-unpacker filled block indices slot-major (idxs[slot*n_longs+li]) instead of the
correct 1.18+ padded order li-major (idxs[li*bpl+slot]) -> scrambled the block grid ->
manufactured fake horizontal stripes + undercounted water 3x. FIXED in both tools
(build (n_longs,bpl) matrix, ravel row-major).
With the FIXED tool, tile 33,33 NEW render = 108,593 water cols, FLAT surface Y78,
SMOOTH bowl (depths 1..12, mean 4.2, only 10% 1-deep) -- matches the pipeline's 108,593
filled cells exactly. Clean full lake, verified by topdown image. OLD (verify_out) =
103,531 cols but 25% 1-deep harsh edges (the real in-game artifact).
THE WORKING FIX (all in place, lake_bowl_carve=true):
 1. core/river_carver_v2.py bowl branch: lake_mask = old_wet | (lkdep>0 within gouge),
    carve bed=water_y - smoothed_lkdep (gaussian sigma lake_bowl_smooth_sigma=4 removes
    the 1:8 staircase), clip>=0 (only lowers).
 2. core/river_carver_v2.py end (step 8b): "lake wins" — river_meta[lake_mask]=CHAN_LAKE
    so rivers within the basin don't steal cells; run_pipeline fills the WHOLE basin to
    the flat spill level (river channels become deep lake water).
 3. run_pipeline.py: snapshot carved lake bed after carve_rivers, re-apply via np.minimum
    as the LAST step before write_tile (survives flow-erosion/peak-crunch/bed-bank smooth).
 config/thresholds.json hydrology_engine.river_geometry: lake_bowl_carve=true,
 lake_bowl_max_gouge=6.0, lake_bowl_smooth_sigma=4.0.
LESSON: VERIFY THE DIAGNOSTIC TOOL FIRST. ~2hrs lost to a tool bug. The pipeline's own
LAKE_DEBUG chain (now removed) is what exposed it: pipeline said 109k wet, tool said 35k.

## (HISTORICAL) RE-LOCK ATTEMPT — superseded by the WORKING FIX above
Implemented fully: (1) carver smooths the 1:8-staircased lkdep (weighted gaussian within
basin, `lake_bowl_smooth_sigma`) before carving bed=water_y-lkdep; (2) run_pipeline
snapshots the carved lake bed right after carve_rivers (line ~227) and re-applies it via
np.minimum as the LAST step before write_tile (after flow-erosion/peak-crunch/bed/bank
smoothing AND the existing _crunch_lock_y + _post_decorate_y locks, ~line 1295). All
flag-gated by lake_bowl_carve (now FALSE, code preserved + inert when off).
VERIFIED on 33,33: still 34957 inland water (vs OLD 56967) -- did NOT fill the walls.
DEFINITIVE ROOT CAUSE (mask probe at z254): the dry "walls" are CHAN_RIVER cells, NOT
CHAN_LAKE. lake_id 9 is a MISLABELED RIVER-VALLEY: precompute draped ONE lake_id over
rivers (water ~70, terrain-following) + a pool (78) + dry land (79) between. lkdep at
the walls is only 1-2 (they're near the bowl's shore in the dist-transform), and the
walls render as river (70) or dry (79), never the lake's 78. No single water level fills
a basin whose geometry demands three. Confirmed both reported basins are river-valleys:
33,33 water surface spans Y64-79, 19,76 spans Y80-111 (31-block range = not a flat lake).
CONCLUSION: in-pipeline carve CANNOT fix this. The fix is at the SOURCE: the lake
DETECTOR (hydrology_precompute.detect_lakes) over-claims river valleys as lakes. Either
(a) tighten lake detection so it doesn't absorb sloped river valleys (regenerates ALL
hydro masks, 5min, affects whole 50k -- risky/broad), or (b) Gaea re-sculpt. Bowl-carve
+ re-lock code preserved flag-gated; it would help a TRUE flat bowl but none of the
reported basins are one.

## River water-walls (18,67 @9444,34603; 16,72; mouth 30,86 @15436,44049)
Same class as lakes: `river_water_y` from per-pixel `water_y_field` (1:8 precompute,
nearest-neighbor upscaled) -> at steep step-downs adjacent columns get water surfaces
13+ blocks apart -> vertical non-spilling water walls. Confirmed in r.18.67 data
(z295: x216 water=114 vs x219 water=127). Pre-existing, terrain-source class. USER
DECISION needed (same as lakes).

## SDD "missing river" at 19,66 — NOT a bug
hydro_centerline.tif is EMPTY for 19,66/18,67/16,72 (all SDD river tiles). The desert
rivers are the user's HAND-PAINTED rivers (hydro_region.png -> hydro_region_overlay).
18,67 renders its painted river; 19,66 simply isn't covered by the paint. strip_rivers
is correctly OFF (default). Fix = paint the river into 19,66 in hydro_region.png if
wanted. Not code.

## Lithology blend (#9/#10) — CONFIRMED WORKING by user (72,68 + 89,52). Don't touch.
Other litho verify tiles were just snow-covered (bad tile picks), not broken.

## Krummholz (#7/#8/#14/#15) — RETUNED (config + code), verifying via KR_DEBUG render
Was: feather 500->550, near_density_mult 1.0 (full density at zone edge), rock/snow
proximity forced krummholz at ANY altitude -> "too low + overdense + replacing all
treeline trees."
Now (config/thresholds.json krummholz): feather_lo 550, feather_hi 565, near_density
0.2, far_density 0.0, density_top_y 700. Code (core/schematic_placement.py):
- `_kr_dens` is now ALTITUDE-based: 1.0 below 550 (regular taiga untouched), 0.2 at 550
  fading to 0.0 at 700.
- Force-small (rock/snow OR altitude ramp) now GATED by surface_y >= feather_lo, so no
  krummholz below 550.
Verify: KR_DEBUG=1 render of 30,23 -> expect krummholz=0 below 550, sparse 550+.

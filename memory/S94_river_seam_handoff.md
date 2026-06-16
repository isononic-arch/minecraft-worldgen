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
  what the user actually asked: a surface-water cell that sits above an adjacent
  LAND bank (min of the 4 neighbours that are LAND = terrain top; water neighbours
  contribute INF) is LOWERED to that bank (never below its bed). => removes the
  EXPOSED-OVER-LAND TOP LAYER(S), keeps the flood body. NOT delete-the-column
  (drained 13,80 dry — the "what the fuck" screenshot), NOT raise-land (walls).
  **LAND-ONLY support is load-bearing:** an earlier version took min over ALL
  neighbours incl. water, which made a ~615-cell edge-perch CASCADE through the
  water and drain the whole sheet (13,80 10081 -> 2943 water). Land-only trims
  only the cells actually perching over a lower bank; flood body stays. Trade-off:
  where the global bake over-levels the interior (13,80 interior 71 over a 69
  bank), the trimmed shore ring sits a couple blocks below the interior = a thin
  2-block waterline step at the bank (water-over-water, NOT the over-land spill the
  user flagged). The real cure for that is the bake not over-leveling (hard).
- **TWO cleanup passes** (ordering matters): Pass 1 runs PRE-crop on the padded
  arrays (true halo -> seam-safe edges) right after bank-taper/despike. Pass 2
  runs POST the final surface_y re-locks (crunch / post-decorate / lake-bed) on
  the cropped inner arrays. WHY both: the re-locks RAISE the bed on some cells
  AFTER Pass 1, so a cell Pass 1 lowered to its bank renders DRY (bed >= water),
  re-exposing an adjacent higher-water cell as a +3 perch (verified world
  6702,41183: rwy=68 yet solid_top=68 dry -> neighbour 71 perches +3). Pass 2
  edge-pads by 1 (replicate) so np.roll doesn't wrap, and FREEZES the boundary
  ring to Pass 1's true-halo result -> no new seam. Pass 1 alone left 131 perch
  on 13,80; Pass 2 targets those.
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
7. **trim with ALL-neighbour support (incl. water)**: a small edge-perch CASCADES
   through the water and drains the whole flood to its lowest outflow (13,80
   10081 -> 2943). Use LAND-only support so trim is local to actual over-land
   spill (see CURRENT STATE).

## THE CORE TENSION (so you don't re-discover it the hard way)
The global level is seam-clean but over-levels (above terrain) in spots. To fix a
spill at the tile level you must EITHER lower the water (per-tile -> seam) OR raise
land (wall). There is no tile-local way to get seam-clean AND contained when the
level is above the ground. The spill-TRIM accepts a (small, terrain-driven) lower
of the overspilled top — terrain is global so it SHOULD stay ~seam-consistent;
verify the seam after rendering. If a heavily-over-leveled tile (13,80) shows a
re-appearing seam after trim, the real cure is the bake not over-leveling there
(hard: needs the carver's flowing-flood logic globally).

## VALIDATION (all LOCAL renders, 0 box spend, Pass-1 land-only trim)
| tile | water | dry-land perch | water-step |
|---|---|---|---|
| 52,53 | 89193 | 0 (0.00%) | 0 |
| 52,54 | 14603 | 0 (0.00%) | 0 |
| 12,80 | 33801 | 17 (0.05%) | 3* |
| 13,80 | 10111 | 131 (1.30%) | 2* |
- 52,53|52,54 H-seam: every RIVER column identical both sides (terrain 67, water
  70=70), 0 water-steps. WATERLINE SEAM = GONE (the saga's goal). The 2/3 flagged
  "water-steps" on 12,80 are LAND terrain micro-steps (70/69), not water.
- 13,80 = OVER-LEVELED OUTLIER: wide flat savanna wash, global bake can't find the
  bank on near-flat terrain -> levels the interior too high (water 71 over 68-70
  bank). 131 small +3 spots. NOT a global problem (its neighbour 12,80 = 17).
- 12,80|13,80 V-seam terrain: 70 solid-steps>=2 (terrain, NOT water) — savanna
  relief / possible terrain seam, SEPARATE track from the water fix.

## INSTALLED / VERIFY WORLD  (`/d/modrinth_vandir/saves/Vandir50k_verify/region/`)
- ALL 4 installed (Pass-1 trim): r.52.53 / r.52.54 / r.12.80 / r.13.80.
- Walk: 52,53|52,54 waterline seam `/tp @s 26954 75 27648`; 12,80|13,80 seam
  `/tp @s 6656 75 41279`; 13,80 perch outlier `/tp @s 6702 75 41183`.

## S94b SURVEY — how widespread (all LOCAL renders, 0 box; tools/diag_river_survey.py)
Mask-level: pre-cleanup perch = **1.0% of river cells in 14/507 tiles** (rare,
localized). Deltas=92 tiles, high-alt(bed>=150)=66, lake-river junctions=70.
Rendered + diagnosed 8 representative tiles (diag_river_survey_render/, installed):
| tile | kind | water | perch | note |
|---|---|---|---|---|
| 89,58 | high-alt | 57396 | 0.20% | big mtn river, fine (water Y157) |
| 69,62 | high-alt | 55146 | 0.25% | fine |
| 79,71 | high-alt+perch | 1654 | 1.09% | small, minor +5 |
| 35,21 | high-alt+perch | 168 | **20.83%** | **WORST: tiny steep channel, +10 at Y285** |
| 20,63 | delta | 42728 | 0.45% | minor +5 |
| 12,82 | delta | 72682 | 0.00% | clean |
| 16,76 | lake | 104818 | 0.42% | lake-shore perch 228 (0.23%) +4 |
| 51,53 | lake | 179981 | 0.05% | lake-shore perch 34 (0.03%) |
- **Lake>RIVER: NOT reproduced** — both big lakes show lake==river at junctions
  (0% lake higher, cascade blend at line 1077-1090 works). The user's "lakes sit
  higher than adjacent rivers" is actually **lake-over-SHORE perch**: lake water
  +4 above adjacent DRY land at the rim (0.03-0.23%). At a checked cell the MASK
  bed=110 but it RENDERS 105/109 -> the lake bowl-carve + Step-9 re-locks reshape
  the bed/shore AFTER lake_wl is fixed, leaving rim cells dry below the water.
  Same root class as the river re-lock perch.
- **High-alt rivers: big ones fine (0.2%); small/steep ones over-level badly**
  (35,21 20.8%). Bank detection in the global bake fails where the bank is
  ill-defined: wide flat washes (13,80) AND steep narrow channels (35,21).
- diag tools fixed: diag_mca_surface_perch Y_HI 140->320 (high-alt water was a
  false zero); diag_lake_river_level reports lake-shore perch + reach-12 junction.
- Walk (verify world): 35,21 worst high-alt perch `/tp @s 18313 290 11026`;
  16,76 lake-shore perch `/tp @s 8421 113 39118`; 20,63 delta `/tp @s 10603 75 32767`;
  89,58 big mtn river `/tp @s 45828 162 29866`.

## TWO REAL FIXES (bake/pipeline level — NOT done, need user go-ahead; risky unsupervised)
1. **Bake over-leveling** (rebuild_river_wl.py band_bank): on flat-wash / steep-
   narrow terrain the cross-section min-adjacent-land bank is too high -> water
   over-levels. Cure: local-terrain-aware bank. Re-bake OOMs locally at scale 1
   (use scale-4 --native to prototype, or the box). Affects ALL tiles -> must
   re-validate the clean ones (52,53) for regression.
2. **Lake-shore perch**: rim cells below lake_wl render dry (bowl-carve/re-lock
   reshape after level set). Cure: either extend lake fill to all sub-level rim
   cells, or re-lock the lake shore terrain >= lake_wl. NOT the river spill-trim
   (that breaks the flat lake surface).

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

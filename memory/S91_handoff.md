# S91 Handoff — 5 pre-50k regressions: ALL FIXED + VALIDATED LOCALLY

**Branch:** `s85-cherry-picks` on top of `6fe1c90`. **ALL 5 REGRESSIONS FIXED + validated locally (dry-runs + real local renders, no boxes).** Committed at end of S91. NOT pushed — push before the box render per the standard flow.
**Session goal (met):** fix the 5 S90 regressions before the full 50k render. Work order (user): assess #2 → #5 → #1 → #3/#4 → local validation only.

**NEXT (box verify render, when user hands Hetzner token):** push branch → render verify set on ccx63 (+armed auto-killer): the S90 lake TPs (33,33 / 19,76 / 20,75 / 38,35 / 62,61 / 36,33), beach TPs (COASTAL_HEATH N + E_TEMP_COAST + 46,51), a forested boundary pair (50,50|51,50), an underwater coastline boundary (38,12|39,12) — collect to `/c`, install to Vandir50k_verify via PowerShell, walk. **50k gated on the walk.**
**Walk attention points:** (a) trees: no half-trees at any border; the one-sided ~10px trunk-free lane on each border's low-side tile should read as natural thinning (canopy still covers it); forests RE-ROLLED everywhere vs S90 (placement RNG stream shifted — not a bug). (b) Rivers world-wide now hold water ≤ real-bank−1 (cap@top) — check rivers still look full enough in steep terrain, not just at lakes. (c) Lakeshore stream entries: flush with the plain, no causeways, no dry swale. (d) Beaches: 64/65 band, tighter spread, no width step at tile borders.

---

## REGRESSION #2 (surface-gen seams) — ASSESSED + FIXED (validation in flight)

**Assessment method:** extended SURF_DUMP hook (`run_pipeline.py`) to also dump `ground_cover` + (env `SURF_DUMP_SCHEM=1`) run Step 8 and dump placements. New tool `tools/diag_veg_seam.py` — 4 probes across a tile pair: density-field step, sblk composition z-scores, GC composition z-scores, placement density + boundary-crossing footprints. Pair used: (50,50)|(51,50), both 100% MIXED_FOREST.

**Findings (BEFORE, diag_veg_seam_out/run_BEFORE.log):**
1. **DOMINANT: `chunk_writer.stamp_schematic` "clips to tile bounds silently"** — 24 tree footprints crossed this single seam → vertically-sliced half-trees lining every forested border world-wide. Real XZ extents: median ~10, max 21 (the 2*center_off+1 proxy badly under-covers). THE user-visible artifact.
2. GC composition: CLEAN (all z < 1.3). sblk major blocks: CLEAN. → "vegetation coverage" in the regression report = trees/schematics, not ground cover.
3. `density_mult` per-tile min-max normalization (schematic_placement.py:1068): measured mild at this seam (1.4× interior variation) but world-wide latent.
4. KARST grove simplex seeded per-tile (line 937) — grove pattern breaks at KARST borders (code-confirmed).
5. Beach `_width_noise` per-tile RNG + per-tile min-max normalization (eco_gradients.py:835) — beach width steps at every coastal border (code-confirmed).
6. **Minor, PARKED:** mud z=+4.9 / clay z=+5.1 sblk step — eco-gradient water-distance fields (riparian/moisture EDTs) are per-tile; localized to river-crossing borders, 1-3% block frequency. Needs eco_grads input haloing — own session if ever visible.

**Fixes landed (working tree):**
- `core/schematic_placement.py`: edge-guard rejection — candidates within 24px of right/bottom edge load the ACTUAL schematic extent (cached) and reject if the stamp would cross the tile bound. Trees never cut; canopy from interior trees still covers the narrow trunk-free lane. (Stamp anchor = corner, one-sided extent — only right/bottom clip.)
- `core/schematic_placement.py`: karst simplex seed per-tile → `GLOBAL_SEED ^ 0xCA757` (coords were already world).
- `core/schematic_placement.py`: density fBm normalization per-tile min/max → fixed ±1.4 range + clip.
- `core/eco_gradients.py` beach block: per-tile RNG width-noise + dither coin → world-coord splitmix64 hash fields, blurred on a padded window (pad ≥ 3σ), cropped, FIXED analytic normalization (std of blurred uniform = (1/√12)/(2√π·σ)). Seam-symmetric by construction.

**Validation in flight:** re-run of diag_veg_seam on same pair → expect crossings=0, density step ≤ interior, GC/sblk unchanged.

---

## REGRESSION #5 (lake retaining wall + trench) — DIAGNOSED + FIX LANDED (validation pending)

**Diagnosis discipline finding: the SURF_DUMP dry-run CANNOT see this artifact** — it early-returns before Step 9, and the wall is built by Step-9 passes that only run on real (non-dry-run) renders. Added env `SURF_DUMP_STEP9_DIR` → dumps `sy_final/rwy/rmeta9/sy_postdec` right before `write_tile`. Rendered (33,33), (19,76), (20,75), (38,35) locally with it.

**Mechanism (reproduced at (19,76): 2496 Step-9-raised cells, max +4):**
- Step 9 escape-fix (run_pipeline ~Pass 1) raises land below adjacent water to water level (5 iters); EDT berm (Pass 2) ramps `water_y − dist` out to 8px → a 1-px-slope engineered containment cone.
- In the v8.12 lake BLEND zone (≤24px of lake) river water_y is deliberately raised toward lake level → cones get built around lakeshore river entries: **wall higher than both water and natural land, dry swale behind** (104/119 trench cells adjacent to a wall). p50 dist-to-river of raised cells = 5.4px.
- Ocean beaches immune: `berm_target > SEA_LEVEL` gate → matches "LAKES only".
- (33,33): clean profile (no wall — radial mean +0.9→+2.5 monotonic). (20,75): natural flat-at-spill shelf at +0.00 with 1-block-deep natural dip 15-30px out (OUTSIDE hydro_lake basin — NOT carve-caused, NOT fixable by flooding; union-fix idea rejected on this evidence).

**Fix landed:** `run_pipeline.py` Step 9 **Pass 2b "rim grade"** (after EDT berm): masked-normalized gaussian (land-only weights) over a `dilate(lake_mask, rim_grade_radius_px=12)` band, blend band cells to the smoothed height, then RE-RUN the Pass-1 escape loop to re-pin any reopened leak (1-px lip at water level instead of a cone). Padded arrays → seam-safe. Knobs: `hydrology_engine.river_geometry.lake_carve.{rim_grade_radius_px=12, rim_grade_sigma=4.0}`.

**REFINED MECHANISM (max-raise cross-sections at (19,76), the decisive evidence):** the wall is a **LEVEE along the lake's inlet/outlet streams**, not a lakeshore ring. On the flat-at-spill plain, stream water_y rides Y96-100 over a Y94-95 plain (3-5 blocks above natural banks); escape-fix+berm then wall the whole corridor flat at water level → "stream on a raised causeway, natural plain below = wall + trench." Two code defects enable it:
1. **ORDER BUG:** the v8.14 spill-cap (Pass 3, lowers water below natural banks) runs AFTER escape/berm (Pass 1/2) — terrain gets raised for high water that Pass 3 later lowers → orphaned levees even OUTSIDE the blend zone (raised cells p50 dist-to-lake = 49px). Fix: reorder cap BEFORE containment.
2. **v8.14 BLEND-ZONE EXEMPTION:** inside 24px of a lake the cap is skipped entirely (S81 cascade visual) → approach water stays 3-5 above the plain. Fix: replace exemption with a floor of `max(natural-bank cap, NEAREST LAKE LEVEL)` — kills the levee (water pinned to plain/lake level) while preserving the S81 lake-above-river cascade by construction (descending water from a high lake is ≤ lake level everywhere).
Rim grade Pass 2b kept as a finishing smoother. Shape-metric note: ring-lip metrics showed ZERO walls ≥+2 at all 5 walked lakes — the levee is WIDE and flat-topped, only visible in raised-vs-postdec masks and raw cross-sections. Diagnose with max-raise cross-sections, not ring profiles.

**THIRD DEFECT (decisive check: levee site dist-to-lake = 59-68px = OUTSIDE the blend zone, yet uncapped):** v8.14 capped water against **pre_carve_y** banks, but flow-erosion + decorate LOWER real banks 2-3 blocks on flat plains → cap anchored to stale heights let water ride exactly that much above the world the player sees. S81 used pre_carve because the cap then ran AFTER escape (current surface poisoned by raises); the S91 reorder makes the un-raised current surface the honest reference. Fix: `_masked_bank_pad` source `_pre_carve_pad` → `_surface_y_pad`.

**FOURTH DEFECT (v4 per-pass bisect instrumentation = the proof; v3 capped the water yet banks stayed at +3):** the **v15 bank-smooth's clamp `np.maximum(_bank_target, _nearest_wy_bank)` ("never below nearest water level") is a RAISER** — banks below the water level get pulled UP to it, and v15 runs in the v13/v16/v15 span BEFORE any containment pass, on UNCAPPED water. The v4 bisect showed ALL +2/+3 levee cells (538+152) and 1801 of the +1s were built in `p0_start → p05_smooth` — not by escape/berm at all. The flat 12-15-cell Y97 corridor = bank-smooth pinning every bank cell within its radius at uncapped water level. Fix: the v8.14 cap moved to **Pass 0.05, the very TOP of the padded block** (before v13 carve-completion, v16 bed melt, v15 bank smooth, escape, berm, grade) — every consumer of water_y now sees capped water.

**v5 VERDICT at (19,76): LEVEE ELIMINATED.** 8326 water cells capped at bank/lake level (`[s91-cap]` log line). Step-9 raises 2496 → **1303**, wall populations collapsed two orders of magnitude (+2: 540→80, +3: 153→23, +4: gone); the surviving 1200 +1s are ordinary escape lips at the waterline. Cross-sections at all 3 original levee sites: streams sit IN the plain (water = bank−1, terrain follows the natural grade); row 505 water naturally below its banks, zero raises. **World-wide behavior note:** the cap@top + post-decorate bank reference applies to ALL rivers — water everywhere is now ≤ real-bank−1 (or ≤ lake level in blend zones); rivers that previously rode high across flat ground settle into their channels. **Junction (62,61) under v5: CONFIRMED CLEAN** — cap touched only 87 water cells (surgical: vs 8326 on the flat-plain tile), 69 raises all +1 waterline lips, monotonic shore profile, river water near the lake = exactly lake level 95 (S81 cascade continuity intact).

(Fix-iteration discipline note: 4 independent defects stacked on one symptom, each root-caused with decisive evidence — reorder ⊕ blend-floor ⊕ bank-reference ⊕ cap-before-bank-smooth. The per-pass snapshot instrumentation (`SURF_DUMP_STEP9_DIR` → `snap_p*` files) is the tool that cracked #4 and stays available.)
**Design constraint discovered (DO NOT raise radius past ~13):** Step 9 may only reshape land within 14px of water — beyond that (a) the S89 post-decorate land restore overwrites the change (dilated-water-zone exclusion = 14), (b) trees CAN be anchored there (placement water buffer = 14) and would float/sink over reshaped ground. Walls 12-24px from lakes at junctions stay ungraded for now — re-walk and only then consider widening (needs coordinated changes to restore-zone + tree buffer).

---

## REGRESSION #1 (underwater tile-boundary seams) — FIXED (validation in flight)

- BOTH per-tile ocean-depth EDT copies live: `generate_columns` (column_generator.py ~1569, the run_pipeline production path, line 192) AND `process_tile_columns_v2` (~1032, the validate_test_tile/check_tile_seams path).
- Fix: optional `height_tile_padded`/`pad_px` kwargs on both; EDT runs on the padded land mask, cropped back. run_pipeline reads a height-only halo via `core_tile_stream.read_tile(pad_px=max(64, transition_px+16), mask_subset=("height",))` with a non-fatal fallback. (validate_test_tile NOT yet wired to pass the halo — validator parity TODO if its reports matter for underwater coasts.)
- Validation in flight: `tools/diag_seam_local.py 38,12 39,12` (top candidate border from a 1:8 scan — 122 seam-prone rows) → expect A.col511|B.col0 ocean steps ≈ interior gradient (≤2), no 15-block jumps.

## REGRESSIONS #3 + #4 (beach width + Y-eligibility) — FIXED (validation pending)

`core/eco_gradients.py` beach block:
- #3: `_dither_width` 3.0× → config `eco_gradients.beach_gap.dither_width_mult` (default **1.5**) → total width ~7.5-17.5px (was 12-28).
- #4: NEW Y-gate `surface_y <= eco_gradients.beach_gap.max_surface_y` (default **65**) in `_bch_eligible` — beach at Y64 OR Y65 only (post-lift shore band), auto-stops inland climb at the Y65 contour. (The "beach.tif ≥ 0.05" eligibility comment in the block was STALE — no Y/beach.tif gate existed in code at all.)
- Plus the seam-safe noise fields (see #2 fixes).

---

## INSTRUMENTATION ADDED (env-gated, off in production)
- `SURF_DUMP_DIR` extension: also dumps `gc_*` (ground_cover); `SURF_DUMP_SCHEM=1` additionally runs Step 8 + dumps `plc_*` placements.
- `SURF_DUMP_STEP9_DIR`: dumps `sy_final/rwy/rmeta9/sy_postdec` immediately before write_tile (real renders only). The ONLY way to see escape-fix/berm/locks without reading .mca back.
- New tools: `tools/diag_veg_seam.py` (4-probe pair analysis), `tools/diag_lake_wall.py` (carve+Step9 replication — NOTE: replication alone showed 0 walls; the real Step-9 dump is authoritative).

## VALIDATION STATE
- **#2 veg pair (50,50)|(51,50) AFTER: PASS.** TRUE clips 24-proxy → **0** (exact, actual extents, one-sided metric). density_mult seam step 0.0073 < interior 0.0095 (world-continuous). GC + sblk majors unchanged-clean. Designed trade-off: one-sided ~10px trunk-free lane on each seam's low-side tile (canopy still reaches the border; bin [-8,0) = 2 vs ~23 far-field). Placement RNG stream shifted by the rejections → forests re-roll everywhere vs S90 renders (not a bug, just different draws).
- **#5 junction (62,61) WITH rim grade: PASS.** 968 cells graded; Step9 raises 2496/max+4 (pre-fix 19,76) → 69/max+1; zero trench; radial profile monotonic +1.04→+1.38. (19,76) A/B re-render IN FLIGHT.
- **#1 underwater pair (38,12)|(39,12): PARTIAL.** Ocean seam steps max 15-block class → max 4, 15/512 rows >2. Residual ROOT-CAUSED: S89 seam-smoothing halo `_sy_pre` (run_pipeline Step 6c2) is raw-LUT — NO ocean-depth correction — so decorate's seam gaussians smooth corrected inner ocean against an uncorrected halo ring (post-decorate floor rose 54→60 vs 53→57 across the seam). QUEUED EDIT: apply the ocean-depth EDT correction to `_sy_pre` (mirror generate_columns) — then re-run pair (expect ≈0).
- Cosmetic QUEUED EDIT: `_bch_hash01` scalar uint64 multiply emits a RuntimeWarning per tile — pre-mask the salted constant as a Python int.
- **#3/#4 beach pair (46,51)|(47,51): IN FLIGHT** (sand Y ≤65 gate + width ≲18px + seam continuity of world-coord width noise).
- Lake survey (pre-fix code): (33,33) clean, (38,35 sea-level) clean (55 raises max +1), (20,75) natural flat-at-spill shelf + 1-block natural dip OUTSIDE the basin (not carve-caused — union-fix idea REJECTED on this evidence), (19,76) = the regression (2496 raised, walls hug river entries).
- **FINAL SWEEP RESULTS (all production-path, run_pipeline-based):**
  - #1 underwater pair (38,12)|(39,12) after BOTH halo fixes: **0/512 ocean rows >2** (max |d|=2 = normal gradient; was 15-block class).
  - #2 veg pair (50,50)|(51,50): **0 true clips** (exact, actual extents); density field world-continuous; GC/sblk clean.
  - #3/#4 beach pair (46,51)|(47,51): band p50 4px; Y-histogram 63/64/65 (+1.6% Y66 from post-paint surface nudges); **sand seam z=−0.1** (continuous). Far-inland sand = COASTAL_HEATH's intentional `sand (noise3)` palette layer, NOT beach.
  - #5 (19,76) flat-plain: levee eliminated (raises 2496→1303, +2/+3 collapsed 540/153→80/23, streams flush with plain). (62,61) junction: 87 cells capped (surgical), cascade intact at lake level.
- **validate_3x3 vs S60 baseline: VALIDATOR-PATH DRIFT, not production regressions.** The validator (`tools/_pipeline_runner.py`) was BROKEN from S72 until S91 (3-target unpack of carve_rivers' 4-tuple → every tile fatal'd; fixed). Its 48_48 flags (bare-dirt on 3 tiles, biome_seam 3.49×) do NOT reproduce in the production path (direct probe: 0 bare-dirt px on (49,48) via run_pipeline). The validator lacks run_pipeline's Step-6c2 padded biome softening + other post-S72 wiring. **Validator-maintenance follow-up (own session): port _pipeline_runner to call run_pipeline._process_tile or re-sync it; re-snapshot baselines after.**
- Instrumentation kept (env-gated, off in production): SURF_DUMP gc/plc extension, SURF_DUMP_STEP9_DIR final+per-pass snapshots, tools/diag_veg_seam.py, tools/diag_lake_wall.py.
- Parked minor: mud/clay sblk z≈5 at river-crossing borders (eco-grads water-distance fields per-tile; 1-3% frequency, localized). S89_* memory files were already dirty at session start (S90 leftovers) — left uncommitted.
- Boxes untouched ($0).

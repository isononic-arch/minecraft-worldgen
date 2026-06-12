# S93 HANDOFF (final) — carver quantization, tree-lane substitution, bowl warp, BED-CACHE MIGRATION

**Sessions S93 + S93b + S93c (2026-06-10/11), branch `s85-cherry-picks`, ship commits `df0345f` (S93b) + `c611c95`/`ddab939`/`5e16860` (S93c).**
Supersedes [S93_wip_handoff.md](S93_wip_handoff.md) (mid-bisect freeze — historical only).

## S93c ADDENDUM (same day, after the S93b install)

1. **High-altitude sweep verdict:** water structure CLEAN at extreme relief — (79,71) descends 295 blocks in monotone bands (1 incoherent / 2009 cells), (30,12) at Y389-520 smooth (55 / 20631). The S93 quantizer holds. **BUT the bed override dug canyons at altitude:** the v17 `river_bed_8k` stores ABSOLUTE MC-Y baked with a **pre-768 LUT (field max 446.6 = old 448-world ceiling)** → dig grows with elevation: ~0 at Y63-95 (lowland validated tiles), 16-20 at Y145, **225-235 at Y500**. S84-era damage, world-wide at elevated painted rivers.
2. **Canyon guard shipped (`c611c95`):** `river_carve.bed_max_extra_depth` (default 3.0) — override digs at most that far below the live-carved surface. Gates: 3 lowland tiles BYTE-IDENTICAL (rwy/sy/rmeta); (79,71) depth p50 16→6 max 10; (30,12) p50 225→8 max 11. The true rebake = bed-builder reconciliation session.
3. **Cross-tile ecotone dither shipped (`5e16860`)** — user bug at (14333,17728): tree/veg swaps truncated at tile seams (surface blocks blended, GC + species swap fields were inner-grid-only). Swap geometry now computed on `biome_grid_padded`, cropped to inner; GC content pick switched from sequential `rng.integers` (stream re-rolls; tile-seeded) to per-pixel world-coord splitmix64. Gate `tools/diag_ecotone_seam.py`: zero diffs beyond 106px of boundary/edge, plc drift ≤0.8%, seam band ~11.5k swaps EACH side; border top-down clean. Walk note: placements near biome boundaries re-jiggle (canopy-exclusion cascade — S91-class re-roll).
4. **Windowed 8k sampling (`ddab939`):** the 3 per-tile order-3 `map_coordinates` calls each spline-prefiltered the full 8k field into a fresh 512MB float64 → workers got HARD-KILLED by Windows under RAM pressure (pipeline exits 0, tile silently missing). Now tile-bbox windows + 40px margin — (62,61) byte-identical. Local box reality: **7.5GB total RAM** → local renders 1 worker; box workers also slimmer.
## S93e2 ADDENDUM — taper FIXED on box, ENABLED via config (variant B), PENDING USER WALK
The first taper install fragmented streams (user: "big regression at the money walk" — (30,12) went 1 connected wet body → 3 ponds; gates had measured rm/rwy ASSIGNMENTS, not rendered water). Box session (1× ccx63, ~50 min, $0 leftover — deleted, 0 servers): two fixes + knob sweep, gated on the new **TRUE-WET CONNECTIVITY** metric (`(rwy>SEA)&(sy_final<rwy)`, components ≥20px must equal pre-taper = 1/tile):
1. **min_tube_carve floor** (`cc1eb9d`): carve floored inside the taper tube → footprint continuous by construction (breaks were where carve depth fell out of the 0.05 threshold at meander corners/step lips).
2. **BFS over paint-skeleton UNION** (`8e66502`): the flow graph SKIPS stretches of the painted skeleton; in those gaps dcl exceeded target but stayed under nodata radius → tube broke. BFS support = graph cells ∪ skeletonize(hydro_region.png==2).
Box sweep: A(1.5/0.37/1.2) pre-union: all broken. Post-union: B2(2.0/0.40/1.3) erased the thin (27,33) stub (comps 0); **B(2.5/0.45/1.4) = 1 component on ALL 6 tiles** + estuary tidal stable + seam 19/19 water-Y. **Shipped: config `river_carve.headwater_taper` enabled with B knobs** (code default stays False — config carries it). Installed box MCAs for (30,12),(79,71),(27,33),(27,34),(62,61),(19,76). Box ops note: snapshot python = `/root/venv/bin/python` (system python3 lacks deps); box log showed `bed cache MIGRATED v17 -> v19` on first render then HIT ✓.

## S93e ADDENDUM (superseded by S93e2 above) — headwater width taper SHIPPED (`2e789c0`), PENDING USER WALK
**The parked headwater-width feature landed.** Width owner chain established: S80-v15 floods every footprint cell → wet width = footprint = painted polygon. Taper = flow-graph-independent GEODESIC dist-from-source (BFS over 8k skeleton pixels; painted flow accumulation resets at stroke joints → useless for width), target_hw = min(1.5+0.37·√d, painted hw), depth capped sub-threshold outside target (valley fills back, no dry moat). Three guards, each gate-earned: TIDAL (no taper ≤ sea+1 — approved estuary fan had thinned 43%), LAKE-OUTLET (tips adjacent to Gaea lakes seed d=300 — `lake_mask` in the bed cache is EMPTY world-wide, all lakes are Gaea `hydro_lake.tif`), NO-DATA (dcl>48 = graph-less painted channel → keep paint width, never dry — (62,61) inter-lake streams died without it). Knobs `river_carve.headwater_taper{enabled,hw_min,k,edge_soft,nodata_dcl}`. 5 gate batteries ((30,12),(79,71),(27,33),(27,34),(62,61),(19,76)); v1 per-tile skeletonize dried (27,34) row 0-1 at the (27,33) seam → v2 global-points basis (the S93d seam law: never derive carve geometry per-tile). Walk TPs: (30,12) `/tp @s 15802 545 6616` then downstream; (62,61) stream `/tp @s 31921 115 31677`; estuary `/tp @s 14249 83 17462`. Residual: graph-less stubs (e.g. parts of (79,71)) keep full paint width until the flow-graph build covers them (own follow-up); fine-tune k/hw_min from the walk.

## S93d ADDENDUM — razor-seam fix (`b1ddef6`)
**USER WALKED RIVERS: "rivers are good now"** — the full river stack (monotone quantization, bowl warp, canyon guard, v17 migration) is user-validated. Remaining river item: headwater WIDTH only (carver width-map session is NEXT — trace paint-polygon→8k-SDF→centerline-EDT→carve-thresholds→bed-override end-to-end, build a per-point width-map diagnostic, then taper).
**Razor-seam root cause (user screenshot 14334,151,17686):** `soften_biome_boundaries`' spray EDT is grid-local; the per-tile 6c.5 soften couldn't see neighbour-tile biomes (dist=inf → can never spray) → assignment-level salt-and-pepper stopped DEAD at tile edges (0 vs 23-40 BA cells/col at the (27,34)|(28,34) seam). Fixed by making the inner biome grid the CENTER SLICE of the 6c2 halo'd soften (was computed then thrown away); per-tile soften = fallback only. Mirrored in both validators. Gates: spray continuity, interior(>96px) 0 diffs, leak gate 0 vs old∪new bands, river sentinel byte-identical. **Spray texture re-rolls world-wide at biome transitions** (same distribution) — only (27,34)+(28,34) re-rendered/installed; other tiles in the verify world carry old spray at their seams until re-rendered.

5. **S93c walk TPs (installed to Vandir50k_verify):** steep descent (79,71) `/tp @s 40933 168 36487`; extreme headwater (30,12) top `/tp @s 15802 545 6616`, mid `/tp @s 15796 521 6352`; cross-tile veg seam (27,34)|(28,34) `/tp @s 14336 80 17728` (user's report spot — look along x=14336).

## WHAT SHIPPED (all walked or gate-passed)

1. **Tree-border lane fix (`0f5ffe1`, user-walked "its fine")** — S91 edge-guard's clipped placements are now SUBSTITUTED: at edge candidates whose schematic extent can't fit, re-roll weighted among entries that DO fit (instead of dropping). Fills the structural border trough left by corner-anchored one-sided extents.
2. **Carver monotone water-y quantization (`0fce570`, user-walked "Seems good… good work")** — per-path σ=8 smoothing then MONOTONE integer quantization with hysteresis (MIN_RUN=12, re-anchor on ≥1.5 drops) along dist-from-ocean order; kills the checkered/floating water at elevation steps ((27,34) incoherent 34→0, water hist {63,64} only, border 23/23).
3. **Lake-bowl depth warp (`df0345f`, PENDING USER WALK)** — the bowl's depth field is `map_coordinates`-displaced with the SAME world-coord simplex warp as the shoreline SDF, so bed terraces arc with the shore instead of long straight x/z ledges. True A/B at (62,61): 83,948 lake-bed cells moved ±1–3, only 79 cells elsewhere. Visual: `diag_s93g/bowl_compare_62_61.png`.
4. **Bed-cache v17→v19 verbatim migration, race-safe (`df0345f`)** — see CRITICAL section below.

## CRITICAL: THE BED CACHE (50k-blocker knowledge)

- **`masks/_bed_cache_v17.pkl` (1.43GB) is LOAD-BEARING and IRREPRODUCIBLE. NEVER delete it.** The current bed builder produces a DIFFERENT, SHALLOWER bed ((62,61) stream depth 4-5 → 1; the (27,34) estuary drops below the river-tag threshold and VANISHES). Code drifted since the v17-era builder; the cache key (paint + `_ensure_caches` source + tunables) never caught it. Reconciling the builder vs v17 output = its own session.
- `_load_bed_cache_from_disk` now MIGRATES v17→v19 verbatim (re-keyed, + `hw_inset_8k: None`) instead of rebuilding. Race-safe: per-PID tmp names; losing workers wait for the winner's v19; **any rebuild while v17 exists raises `_BedCacheRefusal` (fatal)** — including the stale-key path, which re-migrates instead (key hashes `_ensure_caches` SOURCE → any code edit there flips v19 stale; without the refusal that would have silently regressed every river again).
- **The S93b freeze-gate failure decoded:** 4 workers raced the migration on a shared tmp → 3 PermissionError → fell back to rebuild → one OOM'd (`global bed precompute skipped: MemoryError`) → (62,61) rendered ZERO rivers → a rebuild worker SAVED its regressed 1.1GB bed over the good migrated v19. The symmetric-hysteresis quantizer was wrongly suspected; probe on the morning monotone+v17 dumps proved depth was always correct (4366 cells, p50 4 / max 5) → symmetric hunk reverted to pushed monotone.
- **Before any 50k/box run: verify the box log shows `bed cache MIGRATED v17 -> v19` (or `bed cache HIT`) and NEVER `falling back to rebuild` (string removed) or `_BedCacheRefusal`.** Boxes self-heal (snapshot ships v17).
- Healthy sizes: v17 = 1,429,741,075 B; migrated v19 ≈ 1,429,741,090 B. A ~1.1GB v19 is a poisoned rebuild → delete v19, re-warm (single process: `_load_bed_cache_from_disk(Path('masks/hydro_region.png'))`).

## LOCAL RENDER OPS LESSONS
- **4 workers OOM this machine** (8k² float64 allocs in tile hydro). Use `--threads 2` (or 1) for local multi-tile renders; warm the v19 single-process first. Pipeline EXITS 0 even when a tile errors — grep the log for `tile_error` before trusting outputs.
- Verdict battery + gates for this work: `diag_s93g/verdict.py` (scratch), `tools/diag_water_coherence.py`, `tools/diag_bowl_compare.py`, `tools/diag_border_topdown.py`.

## GATE STATE AT SHIP (diag_s93g, warm verbatim v19)
- (62,61): 4366 river cells, skel depth p50 4 / max 5, rwy flat 95, incoherent 0, floating 0; rwy byte-identical to approved morning render; bowl warp arcs.
- (27,34): byte-identical to the user-approved morning estuary render (floating=34 is a pre-existing approved metric quirk at the mouth).
- (27,33): 363 cells, coherent, flat 64; border vs (27,34): presence 23/23, water-Y 23/23.
- (33,33): lake 112,697 cells; bed identical to validated s93d bowl state.

## INSTALLED FOR USER WALK (Vandir50k_verify)
| Tile | What to check | TP |
|---|---|---|
| (33,33) | big lake bowl — terraces should arc/wobble, no straight x/z ledges | `/tp @s 17006 98 17194` |
| (62,61) | lake junction bowl + stream depth (4-5) intact | lake `/tp @s 32044 115 31412`, stream `/tp @s 31921 115 31677` |
| (27,34) | estuary (already approved — regression sentinel) | `/tp @s 14249 83 17462` |
| (27,33)\|(27,34) | seam at z=17408 — river crosses cleanly | `/tp @s 14249 83 17408` |

## PARKED / NEXT
- **Headwater thin→wide:** parked behind `_HW_INSET_ENABLED=False` in `hydro_region_overlay.py`. Rendered width is NOT a single-lever function of the carve SDF (centerline+EDT width radius, polygon SDF, carve thresholds 0.1/0.5, bed override all contribute; two gates gave inconsistent width/depth responses). Needs a dedicated carver width-map session: trace centerline → EDT-width → polygon-SDF → thresholds → bed-override end-to-end FIRST, then re-attempt the inset.
- **Bed-builder reconciliation vs v17 output** (own session; until then the migration is the safety).
- Both Hetzner tokens burned in chat — user to rotate. No boxes alive ($0).

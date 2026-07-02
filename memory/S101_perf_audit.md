# S101 Renderer Efficiency Audit — measured, ranked, whittled down (2026-07-02)

**Ground truth:** cProfile of `tools/validate_test_tile.py` on dense-forest tile (21,30)
(TEMPERATE_RAINFOREST, rivers): **381.7s instrumented** (uninstrumented land tiles run
200-320s; cProfile inflates pure-Python ~2x, numpy much less — use SHARES, not absolutes).
Artifacts: `scratchpad/prof_21_30.pstats`, `scratchpad/prof_21_30.log`, report `scratchpad/vreport_21_30`.
**Caveat:** schematic placement ran with an EMPTY index in this tool (0 placements) —
tree placement + stamping cost is NOT in these numbers and comes on top in production.

## Measured decomposition (instrumented)

| Share | Cost | What |
|---|---|---|
| **61%** | 233.9s | **opensimplex noise2array — 30 calls, 17.9M `_noise2()` evals, 64.6M `_extrapolate2()`** (pure Python; numba NOT installed, unavailable on Py3.14). Callers: `surface_decorator._gen_layer_noise` 16 calls/58.1s + `_noise_tile` 4 calls/38.6s (=> `decorate_surface` 98.4s is ~98% noise), rest inside `assign_biomes` (132.2s cum incl. its noise + soften) |
| **17%** | 65.5s | **matplotlib `points_in_path` — 2 calls** inside `hydro_region_overlay._rasterize_river_edges_tile` (70.3s cum): painted-river polygon rasterization tests 262k tile pixels against a huge path, per tile near painted rivers |
| **14%** | 55.4s | **`chunk_writer.write_tile`** — NBT emit dominates: `_chunk_to_nbt_bytes` 50.3s (np.full 14.0s/50k calls; np.unique+argsort ~18s/58.5k calls ≈ per-section palettes; `_build_heightmaps_nbt` 11.3s incl. np.isin 8.4s; `to_strings` 1.5s) |
| ~2% | 7.0s | `compute_eco_gradients` (EDT total only 1.8s/26 calls — agents overestimated) |
| ~2% | 5.9s | column gen (`build_column_array` 4.5s + `process_tile_columns_v2` 1.4s) |
| <2% | 5.0s | scipy binary_erosion (275 calls) |
| — | 1.4s | bed-cache pickle load |
| — | 4.6s | mask reads (`read_tile` 2 calls) — rasterio opens are NOT a bottleneck |

## Ranked fixes (realistic savings, uninstrumented)

1. **Vectorized OpenSimplex port (exact)** — replace `opensimplex._noise2a`'s per-point
   Python loop with a numpy implementation. Each output point's arithmetic is independent
   → vectorizing across points does not reorder any single point's float ops → **bit-identical
   is achievable and exactly testable** (`np.array_equal` over full fields, several seeds/scales).
   Numba route is dead on Py3.14. Est: **-40..55% on land tiles (~100-140s real)**, also cuts
   ocean tiles (assign_biomes noise runs everywhere). THE headline. Effort: medium (one
   algorithm port + equality harness). Gate: exact-equality test, then a validate_3x3 baseline run.
2. **NBT emit rework (`chunk_writer`)** — tile-scope block palette (kill per-section
   np.unique/argsort/object churn), cached all-air + biome-pattern NBT templates, replace
   50k np.full section inits, vectorize heightmap fluid isin. Output-identical (same bytes;
   verify palette ordering preserved per-section). Est: **-15..25s EVERY tile** (9,409 of them).
   Effort: medium. This was audit agent #2's call and the profile confirms it.
3. **Painted-river overlay rasterization** — swap `matplotlib.path.points_in_path` for a
   scanline/rasterio/shapely polygon fill. Must A/B pixel-equality on real masks; inclusion-rule
   edge pixels may differ → treat as OUTPUT-CHANGING until proven equal. Est: **-30..60s on
   painted-river tiles only** (fires per tile near `hydro_region.png` content). Effort: low-medium.
4. **Long tail, output-identical, do opportunistically** (each <1-3s/tile): dedupe repeated
   noise fields across passes (agents found 4-6 dupes — worth it AFTER #1, shrinks with it);
   consolidate gap-edge EDT calls in surface_decorator (~60-90ms); hoist schematic extent
   cache to load_index; 4 scalar loops in chunk_writer (seagrass/river-cleanup/veg-floater/
   rock-cleanup — real but <0.5s each); per-tile schematic_index.json re-parse (~0.1s).
5. **NOT the bottleneck (agents' guesses corrected by profile):** rasterio per-tile opens
   (4.6s incl. windowed reads), EDT (1.8s), schematic no-repeat scan (didn't register; but
   note the pass was index-empty here — re-profile a placements-on tile before closing this),
   bed-cache load (1.4s), config re-parse, imports.

## Infra findings (from the cloud/tools audit, unprofiled)
- Island render V8 static per-island box assignment idles ~7 boxes for ~30m while Madre
  finishes (~3.4 box-hours/render). Tile-level work queue fixes it; medium effort/risk.
  (For mask BAKES the S101 answer is 1 box + 15 concurrent bakes — proven, 19 min, $1-2.)
- `render_50k_final.sh` renders all 97x97 incl. the 203 island-owned ocean regions →
  skip-list saves ~203 ocean tiles of box time + removes an install-ordering clobber hazard.
- `validate_3x3` is serial per tile (agent claim, unverified) — parallelizing = faster iteration.
- In-island bake: build_lithology + build_terrain_derived run sequentially per island; could
  run parallel (~2-5 min/island saved) — minor, bakes are cheap now.

## Fleet math (mainland 50k, 8xccx63, ~4h/$50 today)
Fixes #1+#2 plausibly take a 250s land tile to ~100-130s and ocean tiles down proportionally
→ **full render ~4h → ~2-2.5h, ~$25-35**. #3 adds savings on river tiles. All #1/#2 gains
are byte-identity-gated before any money render.

## ✅ IMPLEMENTED + BYTE-GATED (S101 close-out, all landed uncommitted)
Four optimizations, every one gated on byte-identical output: (1) `core/fast_simplex.py`
vectorized OpenSimplex (49/49 bitwise vs reference, 37×; installed via class patch in
run_pipeline + validate tools, kill switch VANDIR_FAST_NOISE=0); (2) chunk_writer NBT
emit rework (tile palettes/templates/LUTs, ~9× emit path) + 4 scalar loops
(tools/diag_nbt_emit_equiv.py); (3) hydro_region_overlay exact numpy scanline replaces
matplotlib points_in_path (pixel-identical 5 real tiles + 127 fuzz, 14-100×;
VANDIR_SLOW_RASTER=1 fallback; tools/diag_river_raster_equiv.py); (4) stamp_schematic
index-domain rework (2.05×). END-TO-END GATES: r.21.30.mca md5 IDENTICAL pre/post, both
no-trees AND with-4,898-trees runs. **Measured tile (21,30) dense forest+rivers:
no-trees 37.8s, WITH trees 50.6s uninstrumented (baseline class 200-320s+ → ~5-6×).**
Remaining headroom: stamp_schematic still 40.8s cum instrumented w/ 40.5M name_of calls
(~10s real) — next perf target if wanted; biome-name truncation bug (spawned chip) is
bytes-CHANGING, schedule deliberately and re-capture harness refs after.

## Session context
- The S101 island mask cloud bake (1 box, 15/15 OK, 19 min) + previews/catbox flow lives in
  `islands/_cloud_bake_masks_s101.sh` + `islands/_box_bake_all_s101.sh` + `islands/_bake_previews_s101.py`.
- `tools/validate_test_tile.py` carve_rivers 4-tuple unpack fixed S101 (same S72 drift as
  validate_3x3 pre-S91); its legacy internal `river_water_y` derivation still drifts from
  production `water_y_field` — align at the validator-baseline re-sync.
- Splice audit (separate): mainland deep floor renders at Y-17 (`ocean_decorator`
  `max_depth_for_reshape=80` clip, NOT the spline) → re-scope the -14..-30 spline plan;
  install_sparse is the safe merger; stale madre out/ overlaps 13 mainland LAND regions —
  sweep before any combined install; 5 islands have 50-80-block shelf-amputation walls at
  buffer edges (decide: +1 buffer_tiles / bake-time taper / accept).

# CLAUDE.md — Vandir World Generation Pipeline

*Auto-loaded by Claude Code. Lean operational doc. For strategy, history, and broad context, see `PROJECT_MEMORY.md`. For the physical-realism refactor plan + implementation log, see `PHYSICAL_REALISM_REFACTOR.md` (§18 is the running log).*

**Current state:** Session 81 (2026-05-11) — **VANDIR HYDROLOGY COMPLETE through v8.14.**  Pipeline produces beautiful naturalistic painted rivers + lakes with: (a) spline-fit boundary (no 8k-pixel staircase fingerprint), (b) smoothstep carve depth with explicit plateau (no sigmoid soft tail), (c) inward-bias choke (rivers narrower than painted footprint), (d) WP-style periodic meander, (e) BLEND_DIST=24 cascade at lake-river junctions, (f) WP-style "fix water escaping" iterative escape-fix + EDT berm with stepped 1-block-per-cell falloff out to 8 cells, (g) **v8.14 final cleanup pass: river water_y capped at adjacent natural bank − 1 (with EDT propagation to wide-river interior), BLEND-protected cells exempt** — eliminates "perched water above natural ground" / "wall sticking up out of land" artifacts.  Last user verdict: "Pass working great."  S80 (painted source) handoff: [memory/S80_river_handoff.md](memory/S80_river_handoff.md).  Full S81 progression v8 → v8.14: [memory/S81_river_handoff.md](memory/S81_river_handoff.md).

**S81 KEY DIFFS (read before any river-related work):**
- `run_pipeline.py:152-160` — adds `apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, w)` immediately after `read_tile`. **Without this call the painted overlay is invisible to the carver and you carve the on-disk WP-findPath rivers.** This is the single load-bearing line of S81.
- `core/river_carver_v2.py:816-832` — width comes from `hydro_width` only; legacy `_ORDER_TO_WIDTH` table deleted.
- `core/river_carver_v2.py:931-949` — flat-bottom carve `new_y_f = nearest_avg - 4`; WP-guardrails tapered formula deleted; edge-spillover guard at line ~1090 also removed (was raising carved edges back up to water_y).
- `core/river_carver_v2.py:957` — `footprint = river_full_mask & ~lake_mask & above_sea` (no EDT halo extension).
- `core/hydro_region_overlay.py:_ensure_caches` — global smoothing tightened to σ=1.5 + threshold 0.20 + binary closing radius 1 (preserves thin pixel-editor strokes); erosion removed (`_paint_eroded_8k_cache = _paint_smooth_8k_cache`).
- `core/hydro_region_overlay.py:apply_hydro_region_overlay` — values written to `hydro_centerline/order/width/depth` are tile_streamer-normalised (divided by 255 / 65535) so `_denorm_u8` round-trips cleanly. Without this normalisation, hydro_centerline=1 round-tripped to 255 = the carver's `braid_fill` sentinel and every painted cell got reinterpreted as wide solid braid water.
- `run_pipeline.py:457` — lake `water_y` uses `np.floor` instead of `np.ceil` so wl=63.4 lands at Y=63 (flush with basin rim), not Y=64 (1 above basin).

**S81 STAIRCASE SAGA (RESOLVED at v8.1):** Multiple iterations attempting to remove visible staircase at MC-scale river banks:
- v1 float-passthrough cache (gaussian σ=1.5 → keep float, threshold at 50k): boundary still snapped to 8k pixel grid
- v2 σ=2.5 50k post-bilinear gaussian: small improvement, staircase still visible
- v3 SDF cache (signed distance from paint, σ=0.7 8k smooth, threshold -0.5): integer-step lattice artifacts, wider rivers
- v4 SDF σ=8 + threshold 0: thin rivers ERASED entirely (1px paint has SDF max=0.5, σ=8 washes below 0)
- v5 lake-mechanism: subtract carve depth from cell's OWN surface_y (not nearest_avg), preserving Gaea ±1-2 block terrain noise. Helped wiggle the boundary
- v6 heavy bank smooth (σ=16, zone=24, 3 passes, includes footprint with water_y-1 cap): slightly smoother but threshold contour still visible
- v7: SDF→sigmoid CONTINUOUS carve depth field. Carve applied EVERYWHERE depth>0.05, cell's OWN surface_y as base, water_y_field set on broad zone (~22 blocks past paint). Strict river_meta gate at depth≥1 to prevent over-tagging banks. Boundary is terrain intersection like lakes, NOT a binary threshold contour. Still had minor 8k-lattice fingerprint in carve depth.
- v8: **spline-fit river outline.** `skimage.measure.find_contours(paint_mask, 0.5)` → sub-pixel boundary at 8k. `scipy.interpolate.splprep(s=1.0×len, per=closed)` fits a periodic B-spline. Sampled at ~10× contour length → dense 50k point cloud → `scipy.spatial.cKDTree`. Per-tile: distance via cKDTree.query (analytical curve distance, no 8k lattice); inside/outside via `matplotlib.path.Path.contains_points` union; sigmoid → continuous carve depth. Validated at `memory/s81_v7_vs_v8_compare_51_53.png`. Render time 30 min/tile.
- **v8.1 (LANDED): smoothness=3.0 + inward bias + sharper sigmoid + WP-style periodic meander.**
  - **Smoothness 1.0 → 3.0** in `_build_spline_outline_50k` call — corners noticeably rounder, contour micro-jaggies washed out. (Range: 5+ over-rounds meander bends; 1 = original.)
  - **`_CARVE_INWARD_BIAS = 5.0`** (new constant in `_rasterize_river_edges_tile`) shifts sigmoid centre 5 blocks inward — chokes carved water zone below painted footprint. Pairs with **`_CARVE_SOFTNESS = 4.0 → 2.5`** (sharper sigmoid → less soft tail eats the bias).
  - **Periodic meander** (WP river_script1.7-inspired) added inside `_build_spline_outline_50k`. Defaults: `periodic=True`, `periodic_amp_blocks=6`, `periodic_wavelength_blocks=140`, `phase_distortion_amp_blocks=350`, `phase_distortion_wavelength_blocks=800`, `micro_amp_blocks=1`, `micro_wavelength_blocks=30`. Mechanism: `disp = sin((arclen + simplex_macro × phase_amp) × 2π/λ) × periodic_amp + simplex_micro × micro_amp`. Both banks see correlated displacement → channel meanders rather than fattening/thinning. Phase distortion makes sin non-uniform across world — no tin-spaghetti look. Falls back to two-octave raw noise via `periodic=False`. Validated at `memory/s81_v8_meander_compare_51_53.png` — visible bank undulation + small irregular protrusions on top-right peninsula. **User says: "newest render looks great. Beautiful."**
  - **Tunables for next iteration** (all in `core/hydro_region_overlay.py`):
    - Width: `_CARVE_INWARD_BIAS` (current 4.0 in v8.6+, was 5.0 in v8.1). Higher = thinner; goes too thin past ~7-8.
    - Bank softness: `_CARVE_SOFTNESS` (current 3.0 in v8.6+, was 2.5 in v8.1). Lower = harder edge / more visible choke; don't drop below 1.5 or staircase returns.
    - Corner roundness: `smoothness_factor` in call site (current 3.0). Higher = rounder; ~5+ erases meander bends.
    - Meander amplitude: `periodic_amp_blocks` (current 6.0). Bigger = taller bends.
    - Meander wavelength: `periodic_wavelength_blocks` (current 140). Smaller = tighter cycle spacing.
    - Phase wobble: `phase_distortion_amp_blocks` (current 350). Smaller = more uniform sinusoid; bigger = more random.
    - Bank irregularity: `micro_amp_blocks` (current 1.0). Bigger = noisier banks.

**S81 v8.2 → v8.14 PROGRESSION (post-"Beautiful" iteration log):**
- v8.2 (REVERTED): per-branch arclength water_y + lake snap → 20-block water columns + flat rivers
- v8.3 Step A: skeleton-EDT for `nearest_idx` propagation. Cross-section uneven cells 14k → 958. Kept.
- v8.4: `_CARVE_MAX_DEPTH = 4 → 6` deeper trough. River-paint-wins-over-lake-paint at overlap (`lake_paint = (hr_arr == 1) & ~paint_mask` in `_ensure_caches`). Veg-kill on water-zone + 1-cell buffer in chunk_writer. Kept.
- v8.5 (PARTIAL REVERT): Smoothstep with plateau replaces sigmoid (kept). Step B `_grav_labeled` from skeleton (REVERTED at v8.6 — caused triangle water columns at confluences). Lake containment wall (replaced at v8.6). Underwater grass→dirt swap in chunk_writer (kept).
- v8.6: WP river_script1.7-style iterative escape-fix pass replaces v8.5 lake wall. Generic invariant: at convergence, no water cell has terrain-lower neighbor. `_CARVE_INWARD_BIAS = 5 → 4`, `_CARVE_SOFTNESS = 2.5 → 3`. Kept.
- v8.7: berm-widening pass (3-step stepped falloff). Replaced at v8.8.
- v8.8: EDT-based smooth-slope berm replacing v8.7 stepped widen — `target = nearest_water_y - dist_to_water` for cells within `BERM_RADIUS=8`. Land cells only (`river_water_y < 0`). Escape-fix gated to land cells only (lake water cells left alone for natural cascade). Kept.
- v8.9 (REVERTED slope shift, KEPT density): tried `target = water_y - max(dist-1, 0)` — broke rivers in middle of 3×3, slope shift caused vertical disconnect. Reverted to v8.8 `target = water_y - dist`. BIRCH_FOREST density 0.30 → 0.36 kept.
- v8.10 (REVERTED): rim-cap on lake water_y (Option A) — killed water in painted-river cells inside precompute basins. Reverted.
- v8.11 (REVERTED): paint-aware basin zeroing in carver disk-load path — removed CHAN_LAKE classification at painted-river cells, but the river formula gave too-low water_y for high-elevation rivers → no visible water. Reverted.
- **v8.12 (LANDED): `BLEND_DIST = 8 → 24`** in run_pipeline river-lake blend. Bridges 19-block lake/river elevation gap with smooth gradient. Lake-river east-inlet connectivity restored.
- **v8.13 (LANDED): Bank smooth-brush footprint-only.** `BANK_ZONE` removed from carver section 7.6b — gaussian smoothing now operates ONLY inside footprint cells (carve floor smoothing), not on bank cells. Eliminates the "valley dip" pull-down that the EDT berm couldn't fully compensate for. Bank shaping is entirely escape-fix + EDT berm responsibility.
- **v8.14 (LANDED): final water-level cleanup pass.** In `run_pipeline.py` after escape-fix + EDT berm: cap river water_y at `min(adjacent natural bank) − 1` via `minimum_filter(pre_carve_y, size=3)`. EDT propagation extends cap into wide-river interior so cross-section uniform. **EXCEPTION: cells within 24 blocks of a lake (BLEND zone) preserved** — keeps the v8.12 lake-river cascade visually connected. Lakes (`CHAN_LAKE`) untouched. **User says: "Pass working great."**

**S81 BACKLOG (DEFER until full-world 50k regen):**
1. **Tile-boundary line artifacts** — per-tile gaussian/EDT/3×3-max passes can't see across tile boundaries. Visible at tile seams as "harsh single-X line." Fix: pad surface_y + river_water_y with ~32-48 cells of neighbor data before escape-fix + berm passes. Implementation: read `height.tif` + `hydro_region.png` + `hydro_lake_wl.tif` at padded coords in `run_pipeline.py`; reconstruct approximate `river_water_y` for pad region (painted river → terrain−1, painted lake → precompute_wl, else → −999); run escape-fix + berm on padded array, crop back. ~70 lines. **Test on 2 tiles before full-world.**
2. **Spline cache disk persistence** — currently `_build_spline_outline_50k` rebuilds the cKDTree + meander samples on every fresh process (~10-15 min). For 9409 tiles × 2-4 workers = ~3000 process boots = ~750 hours of redundant work. Pickle `_river_spline_kdtree_cache + _river_spline_polygons_50k_cache + _river_spline_pts_50k_cache` to `masks/_spline_cache.pkl`. Cache key = md5(hydro_region.png bytes) + md5(`_build_spline_outline_50k` source) + tuple of meander params. Atomic write via `.tmp + os.replace`. **Test on 2 tiles before full-world.** Risk: stale cache mid-iteration — DO NOT enable during active spline param tuning.
3. **`OOM with --threads ≥3`** — each worker holds 500-800MB (spline cache + per-tile state). Use `--threads 2` for stability on 16GB-class machines. Could pre-build cache to disk for shared access across workers (links to #2).

**S81 LAKE GEN: also walked through everything in the lake-related path. Lake floor fix (np.ceil → np.floor for water_y in run_pipeline.py:457) preserved. Lakes render correctly: terrain-intersection at flat water plane gives natural smooth shoreline.**

**S81 STEPPABLE BANKS + U-SHAPE FLOOR (still in code):**
- `core/river_carver_v2.py:7.6b` bank smooth-brush (σ=16, zone=24, 3 passes, includes footprint with water_y-1 cap) — heavy WorldEdit-style smoothing
- `core/river_carver_v2.py:7.6c` steppable shore lip (1-cell ring at water_y+1) — wadeable hop-out from water
- WP river_script1.7 audited for U-shape; we diverged (kept water in full footprint, dropped factor*original blend) — see memory/S81_river_handoff.md for line-by-line diffs

**S81 HydrologyPixelEditor (`tools/override_studio.py`):** new `QDialog` opened from a button on the Hydrology tab. Loads `masks/hydro_region.png` at native 8192×8192 — 1 brush pixel = 1 source cell ≈ 6 MC blocks. Backdrop shows ocean (deep blue) + REAL underwater lake cells `(hydro_lake>0) & (height<wl)` (cyan). Brush+eraser tools, B/E hotkeys, undo/redo, save writes 8192 PNG directly with NO upscale step. Use this for lake-tile river connection precision; the main 2048 tab is still better for broad painting.

**S81 SUPERSEDES — these S80 entries are no longer accurate:**
- ~~"All shape post-process at 8k GLOBALLY (v34i)"~~ smoothing parameters changed (σ=4→σ=1.5, threshold 0.30→0.20, erosion REMOVED).
- ~~"EDT-derived per-cell width"~~ — width is no longer EDT-derived in apply_overlay; the carver uses `hydro_width` (which apply_overlay sets to 0 in current code) and falls back to a 2.5-block default. The flow-accumulation widening pass is computed but currently `per_cell_radius` is forced to zero per the user "no width modification" directive.
- ~~"Mouth restore + shore bridge"~~ — mouth restore still present; shore bridge logic removed earlier in S81 then orphan-endpoint extension was also removed.

**S80 CARVER FIXES (still in code):**
- **5a `run_pipeline.py:411-460`** — lake `water_y` is `MIN(ceil(wl_float))` per terrain-intersection connected component.  No force-down of `surface_y`; relies on chunk_writer's natural shoreline when `terrain_int >= water_int`.  Replaced v23's median-ceil + force-down which caused visible spillover and per-pixel ceil's stepped water surface.
- **5b `core/river_carver_v2.py:957-967`** — river `water_y = nearest_avg + min(slope×width, 4) - 1` (bank-relative).  Slope×width estimates bank rise above centerline → consistent ~1-block air gap regardless of bank slope.  Replaced `nearest_avg - 1.5` formula which gave 2-block air gap on tilted banks.
- **Carver gate `core/river_carver_v2.py:254`** — `_has_any_hydro` now includes `hydro_lake.max() > 0`.  Lake-only tiles (no rivers) used to fall to legacy carver which couldn't paint terrain-intersection lakes.

**S80 PAINTED RIVER PIPELINE (v32 → v34j):**
- **`hydro_region.png` IS THE SOLE SOURCE** when paint exists (v32).  In `core/hydro_region_overlay.py`, the precompute centerline/order/width/depth are zeroed in-memory before paint is applied; on-disk TIFs untouched.  Empty paint → falls back to WP findPath.
- **Spline pickle SKIP** — `core/river_carver_v2.py:545-572` skips `river_splines.pkl` loading when `hydro_region.png` contains paint.  Belt-and-suspenders: pickle also renamed to `river_splines.pkl.disabled_v32_paint_only`.
- **Endpoint pruning REMOVED** — `core/region_overlay_smoothing.py:clean_painted_river_mask` no longer prunes endpoints (was chopping ~49 MC blocks off every river tip, causing all painted rivers to dead-end short of any lake/coast).
- **All shape post-process at 8k GLOBALLY (v34i)** — eliminates tile-boundary breaks where per-tile erosion saw "outside-tile = no paint".  In `_ensure_caches`: 8k smooth (gaussian σ=4 → threshold 0.30 → gaussian σ=1 → threshold 0.45) → erosion 2 cells (≈12 MC blocks).  Per-tile rasterize bilinear-samples both `_paint_eroded_8k_cache` (default carve) and `_paint_smooth_8k_cache` (full mask, used by mouth-restore).
- **Mouth restore + shore bridge** — `apply_hydro_region_overlay` post-process: within 80 blocks of any sink (lake / ocean / underwater), restore the un-eroded smooth mask → wide deltas.  `near_river ∩ near_water ∩ basin` adds bridge cells between painted river and visible water within a basin shore.
- **EDT-derived per-cell width** — `_river_width_8k_cache = distance_transform_edt(painted_mask)` × 8k→50k scale.  Wide brush stroke → wide carve; thin stroke → thin carve.  No floor / no cap (per user invariant "no floor for narrow/wide").  Slope modifier: 1.0× flat → 0.6× steep.

**S80 PAINTOVER STATE:**
- `masks/hydro_region.png` — user's painted rivers (id=2 at 8192 resolution, 370k cells across world).
- `masks/river_splines.pkl.disabled_v32_paint_only` — old WP findPath splines (renamed off, do NOT restore while paint exists).
- `masks/river_splines.pkl.s80_legacy.bak` — pre-S80 legacy backup (don't touch).

**S80 OVERRIDE STUDIO (`tools/override_studio.py` Hydrology tab) — v29 → v34:**
- True 1px brush (slider value 1 paints exactly one cell, special-cased to bypass disc math)
- Eraser tool (🧽), Brush (🖌), Fill (🪣) — eyedropper removed (no purpose with single paint id)
- Hotkeys: B=brush, E=eraser
- Three independent overlay toggles (each its own colour): Precompute rivers (river.tif, orange-red) / WP script 1.7 (hydro_centerline.tif, vivid blue) / Real lakes (terrain-intersection, cyan)
- True ocean toggle (height ≤ raw 17050 → deep navy base)
- Gaea flow accumulation overlay (flow.tif, green tiers: tributaries / rivers / trunks)
- Channel-aware bucket fill: click on visible WP/precompute river → fills entire connected channel
- Vivid yellow paint colour (id=2 only) — pops over blue/cyan backdrops
- Clear-all-painting button — wipes buffer + on-disk hydro_region.png
- Performance: backdrop pixmaps cached (skipping rebuild on paint events), line interpolation between mouse events for continuous strokes
- DISPLAY_SIZE bumped 1024 → 2048 for finer paint precision
- Binary mask loader uses `Resampling.bilinear` then 4x max-pool (replaces invalid `Resampling.max` for .read())

**S80 PRIOR RECAP (S72-79):** earlier carver work — gravity flatten, path-smoothing, depth tuning, connectivity layer experiments (REJECTED, archived behind `_CONNECTIVITY_ENABLED=False` at `core/river_carver_v2.py:727-895`).  See [memory/S72-79_handoff.md](memory/S72-79_handoff.md) for details.

**Session 69 (2026-04-22/24) — prior state:** Override Studio tool + overlay-layer pipeline integration + P0 fixes, all merged to master as `f81eb13`.  Three-tab PyQt6 paint studio at `tools/override_studio.py` (Biome / Lithology / Hydrology), with natural brush shapes, elevation-band + ocean/land + rock-gap clamps, scatter brush, 7-band elevation overlay, ocean@Y63 overlay, and write-time validation (zone codes, shape, dtype, NEAREST round-trip, biome-vs-height alignment).  `masks/lithology_region.png` wired through `tools/build_lithology.py`.  `masks/hydro_region.png` wired through new `core/hydro_region_overlay.py` (skeletonize + Bresenham line draw per tile — avoids NEAREST staircase).  User's painted rivers + lithology verified in-world on (60,69) + (89,52) + (51,53).

**S69 P0 fixes landed:**  G1 dune-flatten pass in `core/surface_decorator.py` reverts universal boundary smoother to S67 intensity (sigma=8, passes=3, buffer=24) + locally flattens `gap_mask==8` dune bumps toward neighbourhood baseline.  G2 KARST removed from `SPARSE_BUSH_BIOMES`, `BASE_DENSITY` 0.03→0.20 — verified ~46% bush coverage on (34,9).  Lithology strata tightened: `wave_amp = band_scale_y // 6` (was //3), `col_y_noise ±1` (was ±3), per-voxel 2.5% fleck scatter.  Seagrass-above-water + terrestrial-grass-above-coast cleanup in `chunk_writer.py`.  `BOREAL_ALPINE` zone 40 added to canonical `BIOME_COLORS` + mirror.  Revised 26-biome palette for high contrast across ecological families.

**S69 tool verified end-to-end:**  (60,69) river-only paint → skeleton+line rasterizer produces clean meandering 50k river (was staircased with naive NEAREST upsample).  (89,52) 93% rock exposure, ARCTIC_TUNDRA + BOREAL_ALPINE, lithology should show on cliffs.  (51,53) floodplain + lake, 113k lake px + 103k river px.  See `PHYSICAL_REALISM_REFACTOR.md` §18 S69 entry.

**Next session (S70):** full biome-roster walk via [memory/BIOME_VALIDATOR_CHECKLIST.md](memory/BIOME_VALIDATOR_CHECKLIST.md) + [memory/biome_reference_tiles.csv](memory/biome_reference_tiles.csv).  Then full 50k regen + world overview refresh.

**S62 recap (2026-04-20):** BOREAL_ALPINE altitude-snow attempted fix via per-section biome emit + BIOME_TO_MC_SKY dict.  Later abandoned in S64 for wholesale `minecraft:plains` mapping (simpler, works everywhere).

**S61 recap (2026-04-20):** Schematic placement conform shipped. Two-strategy `stamp_schematic` rewrite in `core/chunk_writer.py:836-1075`: (a) **tree** = post-stamp trunk extension with per-column log type, `MAX_TRUNK_EXT=6`, reject if exceeded; (b) **bush** = placement-level sink if max_gap ≤ 3. Revert of S60-f9 center-sy re-align (back to sample-pixel anchor). 20 `inset_depth=-1` entries normalized to 0 in `schematic_index.json`. `GROUND_COVER_PALETTES` dead_bush rare-ified (max 0.03, was 0.15). Commit `3e4cc92` on master. User validated (25,80) in-world: "everything else looks great."

**S60 prior state (2026-04-19) — preserved for pipeline reference:** Seam routing repair + lithology repaint + query-time Catmull-Rom snow/rock + vegetation palette rewrite + floating-vegetation cleanup + high-elevation stone fade + schematic slope fixes + column-biome fix. See `PHYSICAL_REALISM_REFACTOR.md` §18 S60 entry for details.

**S60 core fixes landed:**
- **Routing repair** (3 changes in `core/schematic_placement.py`): SAND_DUNE_DESERT density 0.01→0.008 + `SAND_DUNE_DESERT` treeline entry `y_top=280 fade=20`; SAND_DUNE_DESERT removed from `NO_BUSH_BIOMES`; post-load mirror copies `SNOWY_BOREAL_TAIGA` entries into `BOREAL_ALPINE` (BA was 0+0 entries, now 29 trees + 27 bushes — was routing drift since S58).
- **Config rename** `"gaussian"` → `"simplex_fbm"` across 112 occurrences in `config/thresholds.json` + back-compat alias in `core/surface_decorator.py:_gen_layer_noise` + `tools/world_studio.py:_gen_field`. Kills the NOISE_PATTERNS.md footgun.
- **Lithology palette repaint** in `config/thresholds.json:3301-3417` per user direction. 6 groups edited, 2 renamed (`sedimentary → arid_basaltic`, `basaltic → temperate_basaltic`). `BOREAL_ALPINE → deepslate_metamorphic` added to `zone_to_group` (was missing).
- **Lithology on rock-gap surface** in `core/surface_decorator.py:1160`: rock-gap (gap==5) surface blocks now per-biome via `zone_to_group`. Previously global stone/andesite/granite/diorite.
- **Catmull-Rom + query-time gap sampler** (new `core/gaea_gap_sampler.py` + `core/upscale.py:_catmull_rom_*`): 8k Gaea slope/dusting sources sampled live at tile time via interpolating Keys a=-0.5 kernel. `config.gaea_gaps.use_query_time=true` default. Threshold + dither are runtime knobs. 5× faster than scipy B-spline zoom. `slope_dither_width 18000→40000 + blue_noise` (breaks up blobby rock interiors). `dusting_dither=none` + warpy threshold (simplex noise at scale=18 src-px, amp=350, downward bias only) — organic snow line. Baked 50k masks retained as fallback.

**Vegetation overhaul** in `core/surface_decorator.py:GROUND_COVER_PALETTES`:
- `resin_clump` removed globally.
- All taiga biomes (BOREAL_TAIGA, SNOWY_BOREAL_TAIGA, BOREAL_ALPINE) density-bumped. BOREAL_ALPINE adds `short_dry_grass` 0.08 + `tall_dry_grass` 0.04.
- 11 biomes gain rare flowers (`oxeye_daisy`, `dandelion`, `poppy`, `allium`, `lily_of_the_valley`, `azure_bluet`, `cornflower`) at 0.005-0.015 per species.
- SCRUBBY_HEATHLAND + EASTERN_TEMPERATE_COAST are "wow damn flowers" biomes — SCRUBBY gets heather/gorse/bilberry color scheme (allium 0.08, dandelion 0.06, azure_bluet/oxeye_daisy 0.05, cornflower 0.04); ETC gets full Cape-Cod coastline rewrite (short_dry_grass 0.50 dominant + bush 0.12 bayberry + 5 flowers at 0.03-0.05).
- SAND_DUNE_DESERT ground cover 5× bumped to counter the 0.05 eco_density_mod suppressor.

**Floating-vegetation + terrain-cap fixes:**
- **Air-below cleanup** (`core/chunk_writer.py:710-737`): existing water-floating check extended to also drop ground_cover with air below. One-line OR condition.
- **High-elevation stone fade** (`core/surface_decorator.py` new post-pass): `surface_y 230→280` ramps grass/dirt/podzol → biome's lithology stone palette. Ground cover zero'd on stone-family surfaces.
- **Schematic ground-touch validation** (`core/schematic_placement.py:766-800`): (a) per-size footprint sy-range reject `_MAX_FP_RANGE_BY_SIZE = {"sm": 4, "md": 3, "lg": 2}` — larger schematics reject more slope; (b) center-re-align uses `_SIZE_CENTER_OFF = {"sm": 2, "md": 3, "lg": 4}` offset from corner to estimate trunk position.
- **Per-column sy in stamp_schematic** (`core/chunk_writer.py:836-895`): underground-cull for non-log blocks uses each column's own sy (`surface_y[tile_z, tile_x]`) instead of single placement center sy. Fixes "tree stuck in ground" on uphill sides. Downhill-floating leaves NOT fixed — carry-forward.

**Column-biome fix** (`core/chunk_writer.py:1069`): removed `if np.all(sec_blk == "air"): continue` early-exit. All 32 sections per chunk emit with biome tag. Biome label correct at any Y including flight altitude. **REQUIRES** `vandir_height.zip` datapack in target world's `datapacks/` folder — otherwise `ArrayIndexOutOfBoundsException: Index 24 out of bounds for length 24` on chunk load (vanilla MC 1.21.10 uses 24 sections; our 32 sections overrun). Datapack was MISSING from `Vandirtest10` mid-session (caused one crash); fixed by copying from `Vandirtest7/datapacks/vandir_height.zip`. Now mandatory-install rule for any test world using S60+ chunk output.

**Validator pipeline tooling (NICK PRIORITY #1 infra DONE):**
- `tools/diag_sbt_presence.py` → `memory/sbt_presence_report.md` (59.8M SBT pixels, 31 regions 1:8-scale).
- `tools/diag_bush_routing.py` → `memory/bush_routing_matrix.md` (0 gaps after Fix C + mirror).
- `tools/diag_biome_sampler.py` → `memory/biome_reference_tiles.csv` + `memory/BIOME_VALIDATOR_CHECKLIST.md` (25/26 biomes have ≥50% pure tile).
- `tools/diag_vegetation_readout.py` → `memory/vegetation_readout.md` (all 26 biomes × ground cover + surface palette dump).
- **`schem_viewer.py` extended** with ground plane + Y-offset slider (-20..+40) + "Save & Approve" (writes `anchor_y` + `anchor_review=false` atomically) + index-only file filter.

**Catmull-Rom A/B diagnostics:**
- `tools/diag_catmull_compare.py` → `memory/catmull_compare.png` (baked A/B).
- `tools/diag_kernel_raw_compare.py` → `memory/kernel_raw_compare.png` (no-dither raw kernel A/B).
- `tools/diag_query_time_tile.py` → `memory/query_time_25_80*.png` (per-tile preview).
- `tools/diag_blob_strategies.py` → `memory/blob_strategies.png` (4 threshold strategies).

**(25,80) S60 render iteration count: 6.** Per-size slope reject + per-column sy landed in final S60 render.

**Prior state (S59, 2026-04-17):** Ground cover + schematic seam dither shipped; vegetation revamp retired. New helper `core/surface_decorator.py:_compute_ecotone_swap_fields` returns shared S58 dither geometry reusable by GC + schematic passes. New `_apply_ecotone_dither_ground_cover` (same 30-block ramp + 0.5 cap + ±20% noise_b, independent coin seed `0x9C0DEC0`) called from `decorate_surface` after `_apply_ground_cover` / before water-cleanup. `core/schematic_placement.py:place_schematics` precomputes per-pixel swap mask + neighbour biome once per tile; overrides `biome_str` at rolled candidates so entries list swaps. All inner-only (no padding). All non-lake rivers scrapped in SAND_DUNE_DESERT via orchestrator strip. §11 Phase 3/4/5 + §6 Pass 3/4 Layer-Protocol vegetation revamp retired to `PHYSICAL_REALISM_VEGETATION_REVAMP_ARCHIVE.md`.

---

## DIRECTION (active)

**Polish + incremental tuning.** S59 retired the three previous strategic bullets:

1. ~~**Surface block selection from physical drivers**~~ — RESOLVED. Surface blocks, beach, snow, windthrow, floodplain, rock, sand dunes all on the S41 Physical Realism Layer pattern. Any remaining noise-as-discriminator is an incremental bugfix, not a strategic track.
2. ~~**Subsurface geology pass**~~ — RESOLVED. `core/column_generator.py` subsurface behavior is acceptable per user review.
3. ~~**Stratification rings bug**~~ — RESOLVED. Legacy stratification system scrapped; the new system in place does not produce the ring artifact.

**Current working backlog** (Claude picks order, user vetoes):

- **🟢 RESOLVED via S80 PIVOT: Connectivity layer no longer needed.** User pivoted to PAINTED RIVERS (`masks/hydro_region.png`) as canonical source. Connectivity is now a function of how the user paints, not algorithm. WP findPath + connectivity layer remain ARCHIVED behind `_CONNECTIVITY_ENABLED=False` (`core/river_carver_v2.py:727-895`) for emergency rollback. Spline pickle renamed to `river_splines.pkl.disabled_v32_paint_only`.

- **🟡 IN PROGRESS: Validate painted-river output in-world (S80 carry-forward).**  Last preview was 3×3 v34i_global around (51,53) — clean tile-boundary connectivity, narrow middle / wide mouth profile. v34j shore-bridge added but not visibly closing the (51,52) outlet gap (user said "little annoying, not a big deal").  Need: render (51,53) MCA + walk in-world. THEN scale to full 50k.

- **🟡 NEXT WIDTH ITERATION: Tributary-aware widening.**  User stated next concern: rivers should widen based on flow accumulation along the painted skeleton (small headwaters, wide mouths). Implementation sketch in `memory/S80_river_handoff.md` — skeletonize → endpoint detection → DFS flow accumulation → per-cell width = base + sqrt(flow_count) × scale.  Apply as POST-process after the existing smoothing.
- ~~**`vandir_height.zip` datapack — MANDATORY install per new world**~~ — RESOLVED S74.  `assets/vandir_height.zip` checked in + `run_pipeline.py:577-595` auto-copies to `output/datapacks/vandir_height.zip` on every run.  When user copies output → world, datapack travels with the MCAs.  Logs "datapack: auto-installed" or warns if assets/ missing.

- ~~**Floating schematics WORSE after S60-f19/f20**~~ — RESOLVED S61. Two-strategy `stamp_schematic` rewrite (tree = per-column trunk extension with MAX_TRUNK_EXT=6; bush = placement-level sink if max_gap ≤ 3). Revert of S60-f9 center-sy re-align. User validated in-world. Commit `3e4cc92` on master.
- **BOREAL_ALPINE altitude-snow follow-up** — S62 landed per-section sky-biome override (`BIOME_TO_MC_SKY = {"BOREAL_ALPINE": "minecraft:plains"}` in `core/chunk_writer.py:78`). Accepted trade-off: 75% of surface cells show plains-tinted grass due to MC 4×4×4 biome cell granularity. If the plains-green grass looks wrong in-world, fallback option: swap BOREAL_ALPINE ground to `minecraft:dark_forest` (temp 0.7, dark-green grass, no snow) — closest warm MC biome to taiga aesthetic. Do NOT add a custom-biome datapack (map is destined for Paper/Spigot server; datapacks break silently on server-admin mis-install).
- **Air-remapped structural gaps in schematics** — `_SPONGE_BLOCK_REMAP` turns 12 fence_gate variants + snow → air; `_CLASSIC_ID_MAP` sends unknown classic IDs → air. Creates gaps in old schematics where structural blocks supported decoration, causing floating leaves/branches. Use the S60 `schem_viewer.py` Y-editor + `anchor_review` workflow to fix schematic-by-schematic, OR audit at load time (flag entries with >5% air-remaps).
- **Ground cover ecotone cross-tile symmetry** — S59 shipped GC + schematic ecotone dither inner-only. 1-pixel seam asymmetry at tile boundaries. Promote to padded after S60 in-world review.
- **NOISE_PATTERNS.md §6 entry** for v12 dither shape — quick doc win (gated on S60-f21 in-world review).
- **`_BIOME_CLIFF_STONE` (`core/chunk_writer.py:95-129`) missing BOREAL_ALPINE entry** — falls through to default, may cause cliff-face seams at alpine boundaries. User flagged "biome seamline on the rock mask in the mountains" S60.
- **RIPARIAN_WOODLAND (zone 80) + FRESHWATER_FEN (zone 240)** have schematic entries but 0 world pixels — wasted routing; prune from index.
- **Phase B `height.tif` regen from `Erosion2_Out`** via new Catmull-Rom — low priority unless user cares.
- **World-wide 50k regen** — ready whenever user calls it. S60 landed significant globals: vegetation overhaul, lithology repaint, query-time rock/snow, column-biome emit-all.
- **S81 v8.13: spline cache disk persistence (FOR FULL-WORLD RENDER)** — currently `_build_spline_outline_50k` rebuilds the cKDTree + meander samples on every fresh process (~10-15 min). For 9409 tiles × even 4 workers = 2350 cache builds = ~470 hours of redundant work. Implement: pickle `_river_spline_kdtree_cache + _river_spline_polygons_50k_cache + _river_spline_pts_50k_cache` to `masks/_spline_cache.pkl`. Cache key = md5(hydro_region.png bytes) + md5(`_build_spline_outline_50k` source) + tuple of meander params. Load on first call per process; rebuild if hash mismatch. Atomic write via `.tmp + os.replace`. Risk: stale cache mid-iteration — DO NOT enable during active spline param tuning. Enable only when params + carve code are stable. Saves ~15min × ~2350 = ~590 hours on full world render.

**S70 backlog additions:**
- **Override Studio `Save` silently skips upscale** — `Ctrl+S` writes `override_final.png` only; `Ctrl+Shift+S` runs upscale. Regular-Save workflow leaves `override.tif` stale by days, which poisons every downstream render (lithology, chunk gen, world map). Fix options: (a) make `Save` always trigger upscale (+5 min cost), (b) add mtime-check warning in `run_pipeline.py` that flags when `override.tif` is older than `override_final.png`. (b) is cheaper; (a) is foolproof.
- **`upscale_override_vectorized.py:272` stale-composite bug** — `np.where(vec_arr > 0, vec_arr, base_arr)` makes `override_vectorized.png` (stale borders file) DOMINATE `override_final.png` (user's fresh paint) wherever vectorized has any value. Since `CONTOUR_SMOOTH = False` (DISABLED), the vectorized file provides zero benefit, just corruption. S70 workaround: `cp override_final.png override_vectorized.png` before upscale (backup kept at `override_vectorized.png.bak_s70_stale`). Real fix: swap composite to `np.where(base > 0, base, vec)` OR delete the composite step and read override_final directly; median filter at 16384 handles smoothing.
- **`hydro_centerline.tif` value 128 + 255 semantics undocumented** — 128 = wadi / dry channel, 255 = braid fill (solid water body). Encoded in `rebuild_centerline.py:85`. World map was dropping 97.5% of river pixels (value 128 + 255 = 16.4M pixels) by only reading Strahler 1-5. Fixed in `tools/diag_world_map_comprehensive.py` S70. Promote to active-mask docs (CLAUDE.md CURRENT PIPELINE STATE + MASK_PIPELINE_REFERENCE.md).
- **`tools/diag_world_map_comprehensive.py` env toggles** — `MAP_IN_GAME=1` renders true carved-footprint widths (no display dilation), `MAP_NO_PAINTED_RIVERS=1` skips `hydro_region.png` overlay. Useful for comparing "map view" vs "in-world view" when validating.
- **`memory/biome_reference_tiles.csv` needs periodic regen against fresh override** — stale CSV says RIPARIAN_WOODLAND + FRESHWATER_FEN absent; post-S70-override regen shows them at tiles (73,53) 51% and (8,74) 45% respectively. Fixed in S70. Add a make target or pre-flight check that regens CSV when `override.tif` mtime is newer.
- **Lithology region-overlay edge smoothing (S70 shipped)** — `core/region_overlay_smoothing.py:smooth_region_paint` now runs on `lithology_region.png` before 8192→6250 NEAREST decimate in `tools/build_lithology.py`. Preserves zero-mask; organic edges via median k=7 + boundary-jitter 4 passes. Same helper available to any future palette-ID paintover (hydro_region categories 1/3/4, for instance).
- **Painted-river cleanup (S70 shipped)** — `core/region_overlay_smoothing.py:clean_painted_river_mask` runs opening + iterative endpoint pruning on painted river mask before skeletonize. Removes brush-dab "four-leaf-clover" artifacts. Applied in both `core/hydro_region_overlay.py` (in-game) and `tools/diag_world_map_comprehensive.py` (map). Defaults: opening_radius=2, prune_max_branch_len=8. Tunable if paint artifacts remain.

**Resolved / struck from backlog (S60):**
- ~~SEMI_ARID_SHRUBLAND sand patches~~ — already-fixed per user; existing `sand (erosion)` layer in `config/thresholds.json:1355` at 38% coverage is acceptable.
- ~~Desert pavement + riparian palette in SAND_DUNE_DESERT~~ — (16,73) in-world review: legacy riparian palette reads as a dry-wadi surface paintover with no depth alteration. Cosmetically acceptable per user. Don't revisit.
- ~~Schematic placement verification on (36,20) / (24,84)~~ — subsumed by the new per-biome checklist (see NICK PRIORITIES).

**NICK PRIORITIES (user-prioritized backlog):**

1. **Per-biome schematic + vegetation placement review** — S60 infra DONE: [memory/BIOME_VALIDATOR_CHECKLIST.md](memory/BIOME_VALIDATOR_CHECKLIST.md) + [memory/biome_reference_tiles.csv](memory/biome_reference_tiles.csv) (25 of 26 biomes have ≥50% pure reference tile; 2 biomes RIPARIAN_WOODLAND + FRESHWATER_FEN absent from world). Walk-in pass NOT YET done; schedule after (25,80) S60 validation lands. Also use the S60 [schem_viewer.py](schem_viewer.py) Y-offset editor to fix any misaligned schematics during the walk.
2. ~~**Snow mask regen without upscaling**~~ — S60 SHIPPED. See §18 S60 entry. `core/gaea_gap_sampler.py` + `core/upscale.py:_catmull_rom_*` + `config.gaea_gaps.use_query_time=true` now sample 8k Gaea slope/dusting live at tile time via Catmull-Rom, no 50k materialization. Threshold + dither mode + interpolation are runtime knobs. Defaults: `catmull_rom, dither=none` (sharp). Baked 50k TIFs retained as fallback. In-world validation at (25,80) pending.

**Open questions surfaced in S60 (file when triaging):**
- "Biome seamline on the rock mask in the mountains" (user observation) — not a mask bug; likely cliff-stone palette transition at biome boundary. See §18 S60 for assessment.
- RIPARIAN_WOODLAND (zone 80) + FRESHWATER_FEN (zone 240) have schematic routing (42 entries) but zero world pixels. Wasted routing.
- `_BIOME_CLIFF_STONE` hardcoded in `core/chunk_writer.py:95-129` has no BOREAL_ALPINE entry — falls through to default. May cause seams at alpine boundaries.
- Catmull-Rom vs cubic_spline with **dither on**: effectively identical (0.09% disagreement). Kernel only matters when dither is off (0.43% disagreement for rock).

**Workflow for this direction:** before editing any `core/` path that no existing 3×3 baseline exercises, snapshot a baseline first (see Workflow rule). Land-heavy reference tiles still missing baselines: `24_80`, `36_20`, `16_73`, `25_72`. Snapshot lazily — only when about to touch a code path that tile exercises.

---

## HARD RULES — DO NOT BREAK

### Override + biomes
1. `masks/override.tif` is the **sole biome source for display**. Never call `assign_biomes()` for world map / cluster / preview — read override + LUT only. `assign_biomes()` runs ONLY inside MCA generation.
2. **NEAREST upscale only** for `override.tif`. Never bilinear/Gaussian on zone codes — intermediate values snap to phantom biomes. Smoothing pipeline: NEAREST→16384, `median_filter(kernel=17)`, NEAREST→50k, light jitter (8 passes).
3. Zone codes come from `core/biome_assignment.py:OVERRIDE_BIOME_MAP`. Never hardcode, never guess.
4. **`BIOME_COLORS` must be byte-identical** between `tools/world_studio.py` and `tools/world_biome_map.py`. Reverse lookup breaks on any RGB drift. Canonical = `world_biome_map.py`.
5. **Never `np.fliplr()`** on the override source PNG. The current backup source is X-correct.
6. **Never modify `override_final.png`** — protected master. Write to `override.tif` only via `upscale_override_vectorized.py`.

### Gap mask
**Current values:** `0`=none, `1`=meadow, `2`=windthrow, `4`=floodplain, `5`=rock, `7`=snow, `8`=sand_dune, `9`=beach. (`3` unused, `6` retired S56 — was alpine_meadow.)

**Application order in `eco_gradients.py`** (each claims `gap==0` unless noted):
1. floodplain (4) → 2. **rock (5) — Gaea slope mask + height fade (Y 150→200) + slope floor (≥18°), claims gap==0 only** → 3. windthrow (2) → 4. meadow (1) → 5. **snow (7) — Gaea dusting mask + peak detector + ridge bias + height fade (Y 250→275), uses `land & ~water & gap!=4`** → 6. sand_dune (8) — overrides 0/1/2 only, NEVER 4/5/7 (S51 fix) → 7. beach (9) — claims gap==0 only, Y=63 constraint.

**Final meadow override** (last pass in `decorate_surface`): dilates 2px, forces `grass_block` on gap **1 and 4 ONLY**. Never include 5/7/8/9 (re-creates staircases).

### Lakes
- Shoreline = **terrain intersection** (`height < spill_elevation`). NEVER morph/blur/spline/gaussian on `hydro_lake` mask.
- `lake_fringe` computed from `river_meta == CHAN_LAKE`, NOT from `hydro_lake`.
- Lake bank width = **2px** (not 6).
- If a lake is wrong, fix it in Gaea, not in post.

### Chunk writer (`core/chunk_writer.py`)
- Biome PalettedContainer **`min_bits=1`** (block states use 4, biomes use 1). Wrong → "Invalid length given for storage" → world fails to load.
- **Top water block per column only** for fluid ticks. Full-column ticks hang MC on ocean tiles.
- **Omit SkyLight/BlockLight entirely.** `isLightOn=0` tells MC to recompute.
- Test world spawn in **void** (~12000, 100, 12000). Approach tiles from outside.
- **`_TEST_SECTION_Y_MAX = None`** — capping at 15 silently drops Y>255.
- Use **`vandir_height.zip`** datapack (`min_y=-64, height=512`). NOT HigherHeightsUltimate4064.
- Block state `[key=value]` must emit a **Properties NBT compound**, not be baked into the Name string. (`_entry()` parses this — don't break it.)
- Leaf blocks need `persistent=true` or MC tick-decays them.
- MC biome `temp ≥ 0.5` for everything except `SNOWY_BOREAL_TAIGA`, `ARCTIC_TUNDRA`, `FROZEN_FLATS`. Otherwise rivers/lakes freeze. Avoid `taiga` (0.25), `windswept_hills` (0.2).

### Mask upscale
- **Gradient masks → bilinear** (windthrow, floodplain, rock_exposure, sand_dunes, snow_caps). Threshold with `> 0.001` afterwards.
- **Discrete masks → NEAREST** (override, lake IDs, lake_wl).

### Surface decoration — Physical Realism Layer Pattern (Session 41 STANDARD)
For HARD geological features (rock / snow / sand / basalt / stratification):
1. PRIMARY drivers = physical signals from `eco_grads`: aspect, north_factor, concavity_norm, wind_exposure, cliff_deg, flow_tile, surface_y. Use hard thresholds.
2. Noise = ±10% edge jitter ONLY. Never the discriminator.
3. **The "decisive" feature must be the LAST assignment** in the function. Steps after it overwrite. (Bug history: basalt at step 5 → 430/441 pixels overwritten by stratification. Move to step 7. Always.)
4. **Distinct blocks per layer.** Two layers painting the same block double-count and dominate (30%+ brown disaster).
5. **Verify thresholds in REAL pipeline**, not standalone test. `concavity_norm` distribution is much narrower in pipeline due to column_generator + river_carver smoothing.
6. **Slope class calibration**: `flat <18°`, `moderate 18-35°`, `steep ≥35°`. Don't use 55°+ from rebuild scripts.

For SOFT organic features (forest floor, moss, grass color), noise IS appropriate. Don't over-apply.

### Noise patterns
**READ `NOISE_PATTERNS.md` before writing any noise/random/probability code for block or ground-cover selection.** Covers salt-and-pepper (per-pixel), fBm simplex blobs, gaussian-filtered lobes, gradient+decision softening, cumulative bands. Critical gotcha: `"gaussian"` in `noise_layers_biome` is actually fBm simplex, NOT per-pixel gaussian — use `"white"` for true per-pixel salt-and-pepper.

### Biome boundaries (S58 + S59)
- **Boundary appearance is decoupled into 3 layers:** (1) `assign_biomes` produces base biome map; (2) `core/biome_assignment.py:soften_biome_boundaries` wobbles the BIOME ASSIGNMENT itself via per-biome simplex noise (scale 200, amp 40) — wide organic curves at the assignment level; (3) `core/surface_decorator.py:_apply_ecotone_dither` adds per-pixel salt-and-pepper BLOCK swap across a 30-block linear ramp at the (now-wobbled) boundary, cap=0.5.
- **S59 extensions:** ground cover gets the same shape via `_apply_ecotone_dither_ground_cover` (independent coin seed `0x9C0DEC0`); schematic placement gets per-candidate entries-list swap via the shared `_compute_ecotone_swap_fields` helper (independent coin seed `0x5C0DEC0`). Surface/sub/GC/schematic = 4 independent coins, same geometry.
- **S59 inner-only limitation:** GC and schematic dither run on inner 512×512 only (no padding). Tile-seam 1-pixel asymmetry is cosmetic carry-forward; promote to padded if visible in-game.
- **Shared helper** `_compute_ecotone_swap_fields(biome_grid, cfg, gap_mask=None, noise_b=None)` returns `(has_neighbour, neighbour_biome, swap_prob_grid, biome_names, width_px, swap_cap)` or None. Returns an (H,W) swap_prob grid (0 outside ramp). Callers roll their own per-pixel coin.
- Cross-tile symmetry: `decorate_surface` accepts `biome_grid_padded` (608×608) computed in each orchestrator's Step 6c2 from a 512-px halo of `{height,slope,flow,erosion,override}`. Same world-coord noise/EDT both sides → seam-symmetric softening.
- **Don't re-enable the gaussian-filtered decision coin** at line 1674 area. S55 v2 used `gaussian_filter(rng.random, sigma=3)` which produced visible perpendicular finger artifacts. S58 reverted to per-pixel uniform random (NOISE_PATTERNS §1+§4). The boundary curvature comes from `soften_biome_boundaries` now, not from coin shape.
- **Alpine biome (zone 40) → `BOREAL_ALPINE`** in `OVERRIDE_BIOME_MAP`. New biome, MC=`minecraft:taiga` (warm, no precipitation snow), palette = exact copy of `SNOWY_BOREAL_TAIGA`. Don't revert to `SNOWY_BOREAL_TAIGA` (S56) without considering: (a) ~1.27M world pixels were snow-forced under S56; (b) intentional cold zones use zone 35 not 40.
- **Dead code kept for re-enable:** `eco_gradients.propagate_biome_downslope` (was Phase 3b v8 alpine inheritance, backed up at branch `backup/s58-v8-inheritance` commit `8e792a8`); `biome_assignment.ridge_watershed_override` (was v9-v10 watershed split). Both removed from runner call sites; functions intact.

### Schematic placement (S58 constraints)
- **Trees skip snow surfaces** — `place_schematics` takes `surface_blocks` arg; checks against `{snow, snow_block, powder_snow, ice, packed_ice, blue_ice}` before placement. Prevents floating-on-snow visual.
- **Per-biome treelines** — `config/thresholds.json:treelines` maps biome → `{y_top, fade_blocks}`. Linear density fade `clamp(1 - (Y - y_top) / fade_blocks, 0, 1)`. Adjust per-biome here, not in code.
- **Slope cutoff** — `eco_placement.slope_penalty_start_deg` (30) and `slope_penalty_full_deg` (45). Trees fade between 30° and 45°, gone above. Tighten if floating-on-cliff returns; loosen if peaks look bare.
- **Schematic loader strips baked snow** — `_SPONGE_BLOCK_REMAP["snow"] = "air"` applied to both `.schem` (Sponge) and `.schematic` (classic) loaders. Don't bake snow into NEW schematics expecting it to render — strip it at the source or override the remap.

### Performance footguns
- **NEVER `binary_closing` with large structuring element** on 6250×6250. Use `binary_dilation(iterations=N) + binary_erosion(iterations=N)`.
- Vectorize labeled-component iteration with `np.bincount(labeled.ravel())`. No `for i in range(n_labels): mask == i`.
- `opensimplex.noise2array` at 1/4 res + bilinear upscale, not native 6250×6250.
- **Slope math gotcha**: `np.gradient(sy)` returns dY per ARRAY INDEX. At 1:8 that's 8× the real slope. `rebuild_sand_dunes.py` uses corrected `/SCALE`. `rebuild_rock_exposure/windthrow/floodplain` + `core/eco_gradients.py` use OLD inflated math with thresholds tuned to it — **don't touch without retuning**. `slope.tif` from Gaea is non-linearly normalized; don't assume `slope_norm * 90 = degrees`.

### Workflow

**Session kickoff pillar checkoff (run this FIRST, every session, no exceptions):**
1. [ ] Read this CLAUDE.md **Current state** line.
2. [ ] Read the most recent `§18 Implementation Log` entry in `PHYSICAL_REALISM_REFACTOR.md`.
3. [ ] Read the referenced §11 phase spec for whatever phase CLAUDE.md + §18 say is NEXT.
4. [ ] **Phase reconciliation check:** does §11's phase spec for NEXT match what CLAUDE.md + §18 describe as NEXT? If NO, STOP — surface the discrepancy to the user and ask which is canonical before writing any code. Do not guess. Do not silently pick one. The S44→S45 handoff drift (Phase 1 meant two different things in two docs) is the exact failure this check prevents.
5. [ ] **Codebase reconciliation check:** grep the exact function names, file paths, and call sites the §11 phase spec mentions. If any named symbol does not exist in the code, OR exists but doesn't have the shape the spec assumes (signature, call graph, what it returns), STOP — surface the mismatch to the user with file:line evidence and ask whether to update the spec or change the approach. Do not write code against a fantasy API. The S46 drift (§11 Phase 1 targeted `column_generator.fill_column()` which doesn't exist; the real mid-column path is `chunk_writer.build_column_array()`) is the exact failure this check prevents. When resolved, mark the old §11 subsection SUPERSEDED verbatim and insert a new decimal phase — never rewrite history.
6. [ ] Replay back to the user: where we left off, what this session must accomplish, what I understand, before executing anything.

**End-of-session reconciliation (run this LAST, every session):**
- Before writing the `§18` entry, paste a three-line phase state block into the entry: "Landed this session: Phase X. §11 currently at: Phase Y. Next session starts: Phase Z." If X/Y/Z disagree with §11's phase list, edit §11 **in the same commit** — add a new decimal subsection (e.g. Phase 0.75, Phase 1.5) rather than renaming existing ones. §11 is the canonical phase map; CLAUDE.md + §18 reference §11 phase numbers verbatim.

**Rule:** §11 is the single source of truth for phase numbering. New intermediate steps get decimal numbers (0.75, 1.5, …) inserted into §11 as new subsections, not by renaming existing phases.

**Operational rules:**
- **Render 3×3 top-down BEFORE generating .mca tiles.** Don't burn 18-min cycles blindly.
- **Always copy generated `.mca` to test world** immediately: `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\` (NOT `.minecraft/saves/`).
- **Failure logging**: after a failed fix, write timestamp + what + why to `memory/project_vandir_status.md` BEFORE retrying.
- **Stop looping**: after 2 failed fixes for the same symptom, STOP. Investigate data with a diagnostic, propose a different strategy, or ask the user.
- **Don't over-research.** 1-2 reference reads max, then write code.
- **Never suggest 50k runs.** Never suggest "save for next session." User decides.
- **Baseline before editing new code paths.** Before touching a `core/` path that no existing 3×3 baseline exercises, snapshot a baseline on a tile that does exercise it. Run `PYTHONUNBUFFERED=1 py tools/validate_3x3.py --tile-x X --tile-z Z --report validation_report_3x3_X_Z`, copy `summary.json` + `report.txt` + `stitched_biomes.png` + `stitched_blocks.png` into `tests/baselines/3x3/{X}_{Z}/`. After the edit, re-run with `--baseline tests/baselines/3x3/{X}_{Z}` and confirm no PASS→FAIL flips. Rule of thumb: snapshot *immediately before* the edit, never prophylactically — baselines rot when palettes/thresholds change legitimately. Current baselines: `48_48` (ocean/coast/seams, clean), `51_53` (rivers/lakes/mixed forest, 8 known pre-existing FAILs — riparian bare-dirt hole in MIXED_FOREST/TEMPERATE_RAINFOREST; still regression-useful because `--baseline` only flags new PASS→FAIL flips). Tiles without a baseline yet: `24_80` (desert/sand/rock), `36_20` (rock exposure/treeline), `59_53` (windthrow), `16_73` (meander), `25_72` (flat sand).

---

## CURRENT PIPELINE STATE

All steps 1-15 complete. In-game validation passed (Session 15). Hydrology engine, river carver v2, ecological surface decoration, terrain-intersection lakes, three-layer alpine, sand dunes + desert rock palette all wired.

**Active masks (50k):**
- `override.tif` (NEAREST) — biome zones (zone 40 maps to SNOWY_BOREAL_TAIGA since S56)
- `height.tif` — terrain
- `flow.tif` — hydrology flow accumulation
- `slope.tif` — Gaea-normalized slope
- `hydro_centerline.tif` — Strahler NMS rivers + braid fill
- `hydro_floodplain.tif` (bilinear) — gap==4 corridors
- `hydro_lake.tif` + `hydro_lake_wl.tif` (float32 NEAREST) — lake basins + spill elevations
- `hydro_width.tif`, `hydro_depth.tif`, `hydro_lkdep.tif` — river/lake geometry
- `wind_windthrow.tif` (bilinear) — gap==2
- `rock_gap.tif` (uint8 {0,1}) — Gaea slope-derived rock mask (S56), gap==5
- `snow_gap.tif` (uint8 {0,1}) — Gaea dusting-derived snow mask (S56), gap==7
- `sand_dunes.tif` (bilinear) — gap==8
- `beach.tif` (bilinear) — gap==9, Y=63 constraint in eco_gradients (S51)

**Rebuild scripts:** `rebuild_centerline.py`, `rebuild_floodplain.py`, `rebuild_windthrow.py`, `rebuild_gaea_gaps.py` (replaces rebuild_rock_exposure.py), `rebuild_sand_dunes.py`, `rebuild_beach.py`, `generate_lake_wl.py`.

---

## KEY FILES

| File | Purpose |
|---|---|
| `run_pipeline.py` | CLI entry, ProcessPoolExecutor tile dispatch |
| `tools/validate_test_tile.py` | Single-tile generator + dry-run report |
| `core/biome_assignment.py` | `OVERRIDE_BIOME_MAP` (canonical zone codes), 4-stage biome logic |
| `core/eco_gradients.py` | 8 ecological gradients, gap_mask logic, alpine_biome_source |
| `core/surface_decorator.py` | Decorate orchestration, `_apply_desert_rock_palette()` line 1617 |
| `core/column_generator.py` | Vertical block columns |
| `core/chunk_writer.py` | nbtlib `.mca` writer (do not break NBT rules above) |
| `core/hydrology_precompute.py` | River/lake extraction at 1:8 |
| `core/river_carver_v2.py` | Spline 50k rasterization |
| `core/tile_streamer.py` | `MASK_NAMES` registration |
| `tools/world_studio.py` | Main GUI |
| `tools/world_biome_map.py` | **Canonical** `BIOME_COLORS` |
| `config/thresholds.json` | All thresholds, source of truth |
| `MASK_PIPELINE_REFERENCE.md` | Quick gap/mask reference |
| `NOISE_PATTERNS.md` | **Noise/dither reference — READ BEFORE writing any noise/random/probability code for block selection** |
| `ARCHITECTURE_VISION.md` | Destination/vision |
| `PROJECT_MEMORY.md` | **Full strategic context, history, broad ruleset** |

**Python:** `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe` — only this install has rasterio/nbtlib/PyQt6/scipy/opensimplex/Pillow.
**Masks:** `C:\Users\nicho\minecraft-worldgen\masks\` (NOT `C:\Users\nicho\masks\`).

---

## TEST WORLD + TILES

**Vandirtest10** at `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\`. Spawn (12000, 100, 12000) void. World height datapack `vandir_height.zip` enabled.

| Tile | Purpose | TP |
|---|---|---|
| (24,80) | Desert rock + alpine reference (current Session 41 work) | `/tp @s 12544 250 41216` |
| (36,20) | Rock exposure / treeline | `/tp @s 18432 200 10240` |
| (51,53) | Floodplain / lakes / schematic ref | `/tp @s 26112 200 27136` |
| (59,53) | Windthrow | `/tp @s 30208 200 27136` |
| (16,73) | Meander reference | — |
| (25,72) | Flat sand desert | — |
| (48,48) | Center sea tile (validator default) | `/tp @s 24832 200 24832` |

**MC version:** 1.21.10 Java, DataVersion 4556. `bush`, `firefly_bush`, `leaf_litter`, `pale_moss_carpet`, `resin_clump` are all valid 1.21.2+ blocks — don't substitute. User rejects `firefly_bush` in forests.

---

## HEIGHT POLARITY (don't get this wrong)
- **HIGH raw 16-bit = HIGH terrain** | **LOW raw 16-bit = ocean floor**
- Sea level = raw 17050 → MC Y 63
- Spline: `gaea_in=[0,17050,45000,65496]` → `mc_y_out=[-64,63,200,448]`
- Y range: -64 (bedrock) to 447 (height datapack).

---

## WORLD CONSTANTS
- 50,000 × 50,000 blocks, 1 block = 1 metre
- 97 × 97 = 9,409 tiles, tile = 512 × 512
- Precompute = 1:8 scale = 6,250 × 6,250
- Tradewind = 270° (west → east)
- Sea level Y=63

---

## COMMANDS (copy-paste ready)

All commands assume CWD = project root. Python = `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe` (aliased as `py` below — substitute if not aliased).

### Validate one tile (single-tile, writes .mca)
```
py tools/validate_test_tile.py --config config/thresholds.json --masks masks/ --output output/ --tile-x 36 --tile-z 20 --report validation_report_36_20
```
- Elapsed: ~3 min on reference tiles
- Exit code: 0 = all PASS, 1 = one or more FAIL, 2 = fatal
- Read `validation_report_36_20/checks.json` → `{"passed": N, "failed": N, "warnings": N}` for machine check
- Read `validation_report_36_20/report.txt` for human-readable summary

### Validate 3×3 (pre-MCA, fast feedback loop — preferred for iteration)
```
py tools/validate_3x3.py --config config/thresholds.json --masks masks/ --output output/ --tile-x 36 --tile-z 20 --report validation_report_3x3_36_20
```
- Runs 9 tiles through the pipeline up to surface decoration. No .mca written, no schematics placed.
- **Always run with `PYTHONUNBUFFERED=1`.** Buffered stdout hides all progress and makes normal slow runs look like hangs. See Session 41 triage.
- **Wall-time budget depends on land fraction, not tile count.** Per-tile reference (measured 2026-04-10 on (48,48)): deep ocean ~24s, ocean+rivers ~60s, mostly-land (80%+ land) ~200-320s (~3-5 min). For 3×3 runs: ocean center ~16 min; land-heavy center (36,20 / 24,80 / 25,72) budget **60 min**, hard ceiling **90 min** before you suspect a hang. Do NOT kill a (36,20) 3×3 at 25 min — that's normal pacing.
- If a run looks stuck: first check per-tile progress lines in stdout; only if stdout has been silent for >10 min AND CPU is idle should you py-spy dump and kill.
- Delta mode: add `--affects-key core/surface_decorator.py` (uses `config/validation_affects.json`)
- Baseline diff: add `--baseline tests/baselines/3x3/36_20` (fails if PASS→FAIL regression)
- Escape hatch for chunk_writer work: add `--full` (runs serial validate_test_tile 9×, writes .mca)

### Validate masks (standalone sanity, ~1 min)
```
py tools/validate_masks.py --masks masks/ --report validation_report_masks
```
- Checks dtype, shape, coverage % bounds for every mask in `config/validation_affects.json → mask_bounds`
- Run after any `rebuild_*.py` before chaining into a 3×3 tile render

### Generate one tile .mca
```
py run_pipeline.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x0 36 --tile-x1 37 --tile-z0 20 --tile-z1 21
```
- Ranges are `[x0, x1)` — exclusive end
- Writes to `output/r.{rx}.{rz}.mca` (region coords, not tile coords)
- ~5-20 min per tile depending on hydro/schematic load

### Copy .mca to test world
```
cp output/r.*.mca 'C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\'
```
NOT `.minecraft/saves/`. Wrong path = silent no-op.

### Rebuild a mask
```
py rebuild_centerline.py      # hydro_centerline.tif
py rebuild_floodplain.py      # hydro_floodplain.tif
py rebuild_windthrow.py       # wind_windthrow.tif
py rebuild_rock_exposure.py   # rock_exposure*.tif, snow_caps.tif, snow_caps_north.tif
py rebuild_sand_dunes.py      # sand_dunes.tif
py rebuild_beach.py           # beach.tif
py generate_lake_wl.py        # hydro_lake_wl.tif
```
Each writes to `masks/{name}.tif` and logs to stdout. Run from project root.

### Top-down preview (fast iteration, no MCA)
See `diag_*topdown.py` scripts. Canonical:
- `diag_layers_breakdown.py` — 12-panel layer debug (~45s, use for stratification work)
- `diag_sand_rock_world.py` — world-scale 1:8 preview (~95s)
- `diag_river_3x3_topdown.py` — 7×3 river carver result

### Precompute mask prerequisites (step0)
```
py step0_diagnostic.py --height masks/height.png --water masks/water.png
```
Writes `step0_output.json` with sea_level + spline breakpoints.

### Interpret PASS/FAIL quickly
```
py -c "import json; d=json.load(open('validation_report_36_20/checks.json')); print('PASS' if d.get('failed',1)==0 else 'FAIL', d)"
```

### Loop rule reminder
- Render 3×3 top-down BEFORE generating any .mca. Don't burn 18-min cycles blindly.
- Log every failed fix to `memory/project_vandir_status.md` BEFORE retrying.
- 2 failed fixes on same symptom → STOP. Investigate, or ask user.

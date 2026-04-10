# CLAUDE.md — Vandir World Generation Pipeline
*Auto-loaded by Claude Code. Last updated: 2026-04-10 (Session 41)*

## CURRENT MASK STANDARD (Session 41 — NEW STANDARD FOR ALL FUTURE MASKS)

**Physical Realism Layer pattern** is the standard for surface block painting. See `memory/feedback_physical_realism_layer.md` for the full pattern.

**Core rules:**
1. Replace noise blob painting with PHYSICAL signals from `eco_grads` (aspect, concavity_norm, cliff_deg, wind_exposure, north_factor) and `flow_tile`
2. Noise is ±10% edge jitter ONLY — never the primary discriminator
3. Layer-by-layer composition with the LAST step being the "decisive" feature
4. Verify thresholds in REAL pipeline, not standalone test (concavity_norm has narrower distribution in actual pipeline)
5. Distinct blocks per layer — no double-counting trap
6. Slope class gating (flat / moderate / steep) for terrain-type-specific palettes

**Reference implementation:** `core/surface_decorator.py:1617` `_apply_desert_rock_palette()`

**Mask generation pattern:**
- 1:8 precompute via `rebuild_*.py` script
- Bilinear upscale to 50k via `write_upscaled()`
- Register in `core/tile_streamer.py` MASK_NAMES
- Wire through `core/eco_gradients.py` (gap_mask) and/or `core/surface_decorator.py` (surface painting)
- Use **weighted SUM** composition for mask scoring, NOT product (Session 39 lesson)
- Smooth biome-driven values (treelines etc.) with gaussian to avoid hard biome-shaped seams

## PYTHON EXECUTABLE
**Always use:** `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe`
No other Python install has the required packages (rasterio, nbtlib, PyQt6, scipy, opensimplex, Pillow).

## MASK FILES
All 50k×50k TIFs live in `C:\Users\nicho\minecraft-worldgen\masks\` — NOT `C:\Users\nicho\masks\`.

## HEIGHT POLARITY (CORRECTED Session 13)
- **HIGH raw 16-bit = HIGH terrain** | **LOW raw 16-bit = ocean floor**
- Sea level = raw 17050 → MC Y 63
- Spline: `gaea_in=[0,17050,45000,65496]` → `mc_y_out=[-64,63,200,448]`
- This matches `step0_output.json` exactly and was confirmed Session 13 by observing
  that ocean pixels have raw < 17050 (low values). The previous inverted polarity
  (`gaea_in=[0,8000,17050,65535]` → `mc_y_out=[448,200,63,-10]`) was wrong —
  it made ocean appear as mountain peaks in the cross-section profile.
- **Display tools updated**: `terrain_preview.py` and `override_aligner2.py` both
  now use normal polarity. Spline editor DEFAULT_PTS also corrected.
- NOTE: The pipeline's block-column generator and biome assignment use
  `config/thresholds.json` — verify that spline there also uses normal polarity.

## OVERRIDE RULES (CRITICAL)
1. **NEVER apply Gaussian blur to zone values** before LUT quantization. Zone codes (0,10,20…240) are discrete labels — blurring creates phantom biomes. Blur only the final RGBA display output.
2. **ALWAYS use NEAREST** for override upscaling. Zone boundary smoothing uses a **2-stage pipeline**: NEAREST 8192→16384, **median filter** at 16384 (kernel=17), NEAREST 16384→50k, then light jitter at 50k (8 passes). The median filter only outputs values present in its input window — no phantom biomes. Never use bilinear/Gaussian on zone codes.
3. **NO `np.fliplr()`** when building override.tif — the backup/canonical source PNG (override_final_backup - Copy.png) is already in the correct X orientation. The old fliplr rule applied to an earlier source that was X-mirrored; the current source is not.
4. **NEVER modify `override_final.png`** — it is the protected master source. Write to override.tif only.
5. `masks/override.tif` is the active 50k×50k override mask. Rebuild with `upscale_override_vectorized.py`.
6. **Override is the SOLE biome source for ALL display** — world map, cluster view, tile preview. NEVER use `assign_biomes()` for preview/display. Read override.tif directly, map zone codes → biome names/colors via LUT. `assign_biomes()` is only for MCA chunk generation. SAND_DUNE_DESERT should be ~6% of land — if you see >10%, something is wrong.
7. **BIOME_COLORS dicts must match** — `tools/world_studio.py` and `tools/world_biome_map.py` must have identical RGB tuples. Mismatch breaks BiomeOverviewLoader reverse color lookup.

## PIPELINE RULES
- All mask reads via `rasterio.Window()` — never full-load a 50k TIF.
- No PyQt6/GUI imports in `core/` or `run_pipeline.py`.
- Always call `process_tile_columns_v2()` — old `process_tile_columns()` is 21× slower.
- `chunk_writer.py` uses nbtlib (not amulet — amulet is not installed). Vol shape must be `(512, h, w)` — shape (384,...) silently produces empty .mca.

## CHUNK WRITER RULES (CRITICAL — hard-won fixes)
1. **Biome PalettedContainer min_bits=1, NOT 4.** Block states use min 4 bits; biomes use min 1 bit. Using 4 for biomes produces "Invalid length given for storage" on every chunk → world fails to load. Fixed in `_pack_indices(min_bits=1)` for `_build_biomes_nbt`. NEVER revert this.
2. **SkyLight/BlockLight omitted.** `isLightOn=0` tells MC to recompute. Do not add light arrays.
3. **Fluid ticks: top water block only.** Pre-scheduling ticks for every water block in a column causes 1M+ ticks on ocean tiles → MC hangs on load. Only tick the topmost water block per edge column.
4. **World spawn for test worlds must be in void**, not inside any tile. Set `level.dat Data.spawn.pos` and `Player.Pos` to an unoccupied region (e.g. 12000, 100, 12000). Approach tiles from outside to allow gradual chunk loading.
5. **dtype=object volume is a memory hog.** `np.full((512,512,512), "air", dtype=object)` ≈ 4.8 GB per tile. With parallel workers this OOMs. Needs int-indexed palette replacement or single-worker mode.

## IN-GAME VALIDATION NOTES (Session 15)
- Tiles (48,48) all-sea, (50,46) coastal/mixed, (56,46) all-land — all loaded and rendered correctly after biome fix.
- Ocean floors are oddly flat — result of min ocean height being pushed down. Cosmetic nitpick, not a blocker.
- Surface block palettes are placeholder/ugly — intentional, to be addressed in architectural vision tooling.
- Schematics not yet rendered in-game — intentional, deferred to architectural vision single-pane tool.
- **IN-GAME VALIDATION IS COMPLETE. Pipeline is ready for architectural vision work.**

## CURRENT PIPELINE STATUS
Steps 1–15: ALL COMPLETE. In-game validation PASSED.
Session 25: Biome Studio, Palette Editor, Norterre pipeline changes ALL COMPLETE.
Session 26: Hydrology Engine, River Carver v2, Layer Stack Palette Editor ALL COMPLETE.
Session 27: Ecological Surface Decoration System ALL COMPLETE.
Session 28: River carver v2 bug fixes, hydrology connectivity, river water fill, bank overhaul ALL COMPLETE.
Session 28 cont.: OOM fix, lake carving overhaul, river smoothing, clay reduction.
- **OOM FIXED**: BlockPalette + uint16 volume (~256 MB vs ~4.8 GB per tile)
- **River smoothing**: bilinear hydro upscale, channel boundary blur, depth rounding, creek tapering
- **Clay <1%** in both river banks and lake fringe; sugar_cane removed from lake fringe
- Hydro masks (width/depth/lkdep) rebuilt with bilinear interpolation
Session 29: **LAKE SEAM FIX** — eliminated all tile-boundary seams in lake carving.
- **Stochastic rounding**: world-space deterministic hash replaces per-tile RNG
- **Lake bank**: narrower taper (4px), richer block dither, tall_grass/short_grass matching rivers
- Small river fragments absorbed into lakes; single-pixel centerlines filtered
Session 30: **LAKE TERRAIN INTERSECTION** — replaced morph+blur with topology-driven lake boundaries.
- **Old approach (REMOVED)**: morph close/open + Gaussian blur on hydro_lake.tif binary mask. Produced straight-line artifacts from 8x8 NEAREST upscale. Increasing morph_r/sigma just widened dry band. Spline smoothing also tried and reverted.
- **New approach**: Lake shoreline defined by terrain intersection — `height.tif < spill_elevation`. The Gaea heightmap at 50k resolution has natural erosion-simulated slopes, so the shoreline follows organic terrain contours with coves, inlets, and peninsulas. No morph, no blur, no splines.
- **New mask**: `hydro_lake_wl.tif` (float32) — stores per-lake spill elevation (normalised height) computed in `hydrology_precompute.py`. NEAREST upscaled to 50k (value is constant per lake, staircase irrelevant).
- **Basin expansion**: 32px dilation of hydro_lake mask, water level propagated via maximum_filter. Ensures terrain intersection extends past the 8x8 staircase edge. Terrain clips it naturally.
- **Depth**: terrain-shaped — `(water_level - terrain) / max_natural_depth * target_depth`. Preserves irregular terrain contour instead of circular EDT rings. `lake_min_center_depth` default 15 blocks.
- **Seam-free by construction**: pure pixel comparison (height < water_level), no convolution, no per-tile processing. Both tiles read same global values at boundary.
- **Config**: `lake_basin_expand_px` (default 32), `lake_min_center_depth` (default 15.0). Old morph_r/sigma/threshold/taper params removed.
- **Pipeline files changed**: `hydrology_precompute.py` (lake_wl output), `tile_streamer.py` (reads hydro_lake_wl), `river_carver_v2.py` (terrain intersection), `run_pipeline.py` (passes height_norm + hydro_lake_wl)
- **Helper**: `generate_lake_wl.py` — builds hydro_lake_wl.tif from existing masks without re-running full precompute
- **Shore dither**: carve threshold 0.5→0.75 (slight reduction of sub-block noise at waterline)
- **Lake bank width**: reduced from 6→2px. Terrain intersection handles shoreline; wide bank was overwriting ecotone.
- **Lake bank grass**: bank pixels get short_grass (sparse), tall_grass (rare). Bank grass cleared within 1px of water to prevent floating.
- **Floating vegetation fix**: post-placement pass clears ALL ground_cover on `river_meta > 0` pixels + 1px margin. Prevents grass appearing to float over adjacent water.
- **CRITICAL chunk_writer block state fix**: `_build_block_states_nbt._entry()` now parses `[key=value]` from block names into NBT Properties compounds. Previously `tall_grass[half=upper]` was baked into Name string — MC didn't recognize it. ALL double-tall plants were silently broken since Session 26. Now correctly emits `{Name: "minecraft:tall_grass", Properties: {half: "upper"}}`.
- **Lake fringe fix**: `eco_gradients.compute_eco_gradients` now takes `river_meta` and computes `lake_fringe` from actual `CHAN_LAKE` water pixels, not the full `hydro_lake` basin mask. Previously the entire basin (47% of tile) got riparian surface treatment.
- **Ecotone dither moved AFTER river banks** so it blends the bank→biome surface block boundary.
- **Biome freeze fix**: remapped cold MC biomes — TEMPERATE_RAINFOREST→dark_forest, BOREAL_TAIGA→old_growth_birch_forest, COASTAL_HEATH/SCRUBBY_HEATHLAND→plains, KARST_BARRENS→savanna_plateau. Only SNOWY_BOREAL_TAIGA/ARCTIC_TUNDRA/FROZEN_FLATS remain snowy.
- **Ground cover recalibration**: tall_grass 0.01 (1%) in all non-tropical biomes (was 0.15-0.38 but invisible pre-NBT-fix). short_grass boosted ~30% across all biomes. Tropical (jungle/mangrove/tidal) retain higher tall_grass.
- **Seagrass safety**: only placed when block above is confirmed water in volume.
- **Lakes DONE** — terrain intersection validated in-game Session 30
Session 32-33: **GLOBAL NMS CENTERLINE PRECOMPUTE** — eliminated river tile-boundary seams.
- **Problem**: Per-tile NMS on flow.tif produced 24-147px seam mismatches. Padded NMS with scipy boundary effects couldn't eliminate seams.
- **Solution (Option D)**: Precompute NMS + suppression + braid fill GLOBALLY at 1:8 (6250×6250) in `nms_centerline()`. Save as `hydro_centerline.tif`. Per-tile carver reads precomputed corridor — zero seams by construction.
- **river.tif integration**: Channels in Gaea's river.tif but not hydro_order (thin coastal tributaries below 1:8 Strahler threshold) included in corridor + braid zone detection.
- **Braid fill**: Detects braided zones from ORIGINAL river_mask density (BEFORE NMS). Morphological closing fills between outermost channels. NMS suppression skipped in braided zones.
- **Straggler removal**: Post-closing blob removal with bincount + river_mask adjacency check.
- **Boundary smoothing**: Gaussian smooth (sigma=1.5) at 1:8 before NEAREST upscale to 50k.
- **Legacy fallback fix**: `carve_rivers()` now checks both `hydro_order` AND `hydro_centerline` before falling back to legacy carver. Previously tiles with only river.tif data (no hydro_order) bypassed precomputed centerline entirely.
- **New mask**: `hydro_centerline.tif` (uint8) — values 1-5=Strahler NMS, 255=braid fill (solid water)
- **New script**: `rebuild_centerline.py` — fast rebuild (~80-107s) from existing masks. Reads at 1:8, runs NMS, writes at 50k.
- **Diagnostic scripts**: `diag_centerline_compare.py` (red/blue/green compare), `diag_river_path.py` (terrain-shaded world overview with terrain-intersection lakes), `diag_river_3x3_topdown.py` (7×3 tile carver output)
- **tile_streamer.py**: Added `hydro_centerline` to MASK_NAMES
- **run_pipeline.py**: Passes `hydro_centerline` to carve_rivers()
- **Config params**: `braid_density_thr=0.015`, `braid_density_sigma=6.0`, `braid_close_radius=10`, `global_smooth_sigma=1.5`, `global_river_tif_thr=0.15`
- **Next milestone: In-game river test → tree schematics → full 50k run**

## HYDROLOGY ENGINE (Session 26+)
- **Precompute**: `core/hydrology_precompute.py` — runs at 1:8 (6250×6250), ~5 min
- **Output masks**: hydro_order.tif (Strahler 1-5), hydro_width.tif, hydro_depth.tif, hydro_lake.tif, hydro_lkdep.tif, hydro_lake_wl.tif, **hydro_centerline.tif** (Session 32-33)
- **River carver v2**: `core/river_carver_v2.py` — precomputed-mask river carver
- **Pipeline**: run_pipeline.py imports river_carver_v2, tile_streamer reads 7 hydro masks (incl. centerline + lake_wl)
- **Falls back** to legacy carver only if BOTH hydro_order and hydro_centerline are absent
- **Config**: thresholds.json `hydrology_engine` section (river_geometry, rainfall_proxy, lake_detection)
- **World Studio**: HydroOverlayLoader (2048×2048 RGBA), "Hydro" toggle, "Reload Hydro" button

## PALETTE EDITOR (Session 26 — Layer Stack)
- **Replaces** old table+slider PaletteEditorWidget with layer stack cards
- **Per layer**: enable checkbox, block dropdown, noise type (gaussian/simplex/voronoi/mix), coverage slider, scale slider, solo checkbox, +/- reorder, Del
- **Base layer**: always at bottom, can't be deleted/reordered
- **Global vs Per-Biome** mode toggle
- **Preview**: 128×128 internal render, bilinear upscale to 380×380
- **Performance**: cached noise fields, only regen changed field (~130ms), debounced 80ms
- **NOT wired into pipeline** — saves to `noise_layers_biome`/`noise_layers_global` in thresholds.json but surface_decorator.py still reads old format
- **Subsurface editor** planned as separate tool (see memory/project_subsurface_editor.md)

## SPLINE EDITOR CHANGES (Session 26)
- Sea-level point Y locked at 63, **X-axis now draggable** (adjusts sea_level_16bit on Apply)
- **Live preview**: `spline_previewed` signal rebuilds LUT + cross-section during drag, no Apply needed
- User adjusted spline to raise coastal lowlands

## WORLD STUDIO OVERVIEW (Session 26)
- **Overview resolution**: 97×97 → 2048×2048 with hillshade (satellite-like, ~25s load)
- **LOD tile system**: disabled (thresholds raised to 9999) — high-res overview sufficient
- **Hydro overlay**: z=10 (above LOD tiles), always visible at any zoom
- **Cross-section persistence**: clicking new tile no longer resets to top-down view
- **Height layer button**: respects current layer when overview loads late

## VANDIRTEST5 WORLD (current test world)
- Path: `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest5\`
- Spawn set to (12000, 100, 12000) — void, safe to load
- Test tiles: r.48.48.mca, r.50.46.mca, r.56.46.mca
- TPs: `/tp @s 24832 200 24832` (sea) | `/tp @s 25856 200 23808` (coastal) | `/tp @s 28928 200 23808` (land)

## VANDIRTEST7 WORLD (Session 28 — inland river test)
- Test tile: (52, 53)
- TP: `/tp @s 26624 200 27136`
- Purpose: validate inland river carving, water fill, bank textures, seagrass placement

## VANDIRTEST8 WORLD (Session 28 cont. — lake overhaul test)
- Test tiles: 50-52 × 52-54 (9 tiles covering lake area)
- TP: `/tp @s 26394 200 27372` (main lake view)
- Purpose: validate lake smoothing, parabolic bowl depth, flat water, creek tapering
- Known issue: river-lake water level step at (26574, 109, 27225)

## VANDIRTEST10 WORLD (Session 30 — terrain intersection lake, latest)
- Test tile: (51, 53)
- TP: `/tp @s 26606 200 27071` (lake shore)
- Purpose: terrain intersection lakes, NBT block state fix, ground cover recalibration
- **Current latest test world** — use for river work next

## SESSION 15 CHANGES
- Identified Modrinth save path (not .minecraft/saves)
- Fixed biome PalettedContainer min_bits (was 4, must be 1 for biomes)
- Fixed fluid ticks (surface-only, was generating 1M+ ticks for ocean tiles)
- Set world spawn to void for test worlds to avoid hang-on-load
- In-game validation complete — pipeline greenlit for architectural vision

## SESSION 13 CHANGES
- `upscale_override_vectorized.py`: added boundary jitter (JITTER_PASSES=3, JITTER_PROB=0.5,
  JITTER_SEED=42) applied at 8192 source resolution before bilinear upscale. Only swaps
  boundary pixels to existing neighbour zone codes — no phantom biomes.
- `upscale_override_vectorized.py`: added FLIP_Z=True and ALIGN_SCALE=1.01 from aligner session.
- `tools/override_aligner2.py`: new drag-to-align tool. LEFT-drag moves override, RIGHT-drag
  pans view, zoom dropdown (1×/2×/4×/8×) for fine alignment. Replaces slider-based aligner.
  Save button runs full 50k rebuild with jitter inline (does NOT call upscale script).
- `tools/terrain_preview.py`: fixed height colormap normalisation — sea level (Y=63) now
  anchors at norm=0.22 in cm.terrain so land/ocean are visually distinguishable.
- `tools/terrain_preview.py`: biome + surface_block render modes now use median_filter(size=11)
  before colorising to remove jitter scatter speckle from display (data unchanged).
- `tools/terrain_preview.py` + `override_aligner2.py`: HEIGHT LUT corrected to normal polarity
  (see HEIGHT POLARITY section above).

## VALIDATOR COMMAND (tile 48,48 = center test tile)
```bash
python tools/validate_test_tile.py --config config/thresholds.json --masks masks --schem-index schematic_index.json --output output --tile-x 48 --tile-z 48 --report validation_report --dry-run
```
Expected: 8 PASS, 0 FAIL, 2 WARN, ~80s.

## SESSION 41 CHANGES (2026-04-10) — SAND DUNES, DESERT ROCK, PHYSICAL REALISM PATTERN

### Sand Dunes Mask (`rebuild_sand_dunes.py` NEW)
- Precomputed at 1:8, bilinear upscale to 50k
- Weighted SUM composition: `slope_gate * biome_weight * (0.4*basin + 0.2*wind_lee + 0.2*concavity + 0.2 baseline)`
- Multiplicative GATES (slope, biome) + additive features
- Slope math FIX: this script uses `np.gradient(sy)/SCALE` (correct) — older rebuild_*.py have inflated math but work because their thresholds are tuned to it
- Biome bleed: SAND_DUNE_DESERT 1.0, DESERT_STEPPE_TRANSITION 0.4, SEMI_ARID_SHRUBLAND 0.2
- Output: 12.1% of land at >=0.20

### Sand Dunes Surface (gap==8)
- Pure `sand` block — no red_sand variants (rejected as "blobs")
- SAND_DUNE_DESERT and ALPINE_MEADOW biome palettes simplified to single base block (was 8-11 noise layers, also created blobs)
- gap==8 priority: overrides gap==0/1/2/4 but NOT alpine/rock/snow
- Sand-flow proximity dither: rock pixels within 4px of sand get probabilistic sand patches (35% at d=1)

### Desert Rock Palette (`_apply_desert_rock_palette()` in surface_decorator.py:1617)
- Replaces stone/andesite/granite for rock pixels in desert-family biomes
- 7-layer composition (see `memory/feedback_physical_realism_layer.md`)
- Block palette: terracotta + brown_terracotta + orange_terracotta + basalt
- **basalt cap rock** = 14.3% of rock pixels (concavity-driven volcanic dikes)
- Wash channels via flow.tif (smooth_sandstone)
- Desert biome detection includes ALPINE_MEADOW (so alpine in desert region inherits)
- **CRITICAL ORDERING:** basalt MUST be the LAST step in the function. Originally placed at step 5, the stratification at step 6 overwrote 430 of 441 basalt pixels. Moved to step 7, after stratification + subsurface.

### Physical Realism Layer Pattern (NEW STANDARD)
- Documented in `memory/feedback_physical_realism_layer.md`
- Replace noise blob painting with PHYSICAL signals from eco_grads
- Noise is ±10% edge jitter only
- Apply to ALL future surface mask painting
- Wind blast layer + aspect shaded layer DOUBLE-COUNT if both paint same block — caused 30%+ brown dominance, fix is to use distinct blocks per layer or drop one
- Real pipeline ≠ standalone test — concavity_norm distribution narrower in real pipeline due to column_generator + river_carver smoothing surface_y

### Treeline Smoothing in `rebuild_rock_exposure.py`
- Per-biome treeline values differ by 110+ Y blocks → hard biome-shaped seams in `rock_exposure_tight.tif`
- Fix: `gaussian_filter(treeline, sigma=15.0)` after biome assignment
- Smooths over ~30 source pixels (240 blocks)

### Slope Class Calibration in surface_decorator
- Reduced steep threshold 55° → 35° (at tile resolution, real slopes peak around 30-40°)
- The 55° threshold was producing only 485 "steep" pixels (0.4%) — way too sparse for stratification

### Alpine Biome Inheritance Extended
- Previously remapped only `gap_mask in {5,6,7}` pixels → nearest non-alpine biome
- Now also remaps any `biome_grid == "ALPINE_MEADOW"` pixel regardless of gap
- Eliminated visible "single band" seam where ALPINE_MEADOW met SAND_DUNE_DESERT at low elevation

### Diagnostic Scripts Added
- `diag_layers_breakdown.py` — fast 12-panel layer breakdown for one tile (~45s)
- `diag_sand_rock_world.py` — world-scale 1:8 preview of sand/rock/snow masks (~95s)

### Memory Files Added
- `memory/feedback_physical_realism_layer.md` — pattern for all future surface painting
- `memory/project_desert_rock_painting.md` — Session 41 detail reference
- `memory/feedback_staircase_catalog.md` (Session 40) — extended with Class 8 (slope unit)

### Known Issues — Stratification "rings around hills"
The stratification bands use absolute `surface_y` for the index. From above this produces correct horizontal contour bands. **In-game ground view**, this appears as concentric rings wrapping around hills. Need to redesign band axis (distance-from-ridge or column-generator integration). **PRIORITY for next session.**

## SESSION 28 CHANGES (2026-03-31) — RIVER CARVER V2 FIXES, WATER FILL, BANK OVERHAUL

### River Carver v2 Bug Fix
- **`propagation_radius` UnboundLocalError**: meander code from a crashed session was inserted before the variable was defined. Fixed by reordering so `propagation_radius` is assigned before use.

### Hydrology Connectivity — Option C
- `enforce_connectivity()` now extends rivers that are order >= 2, OR any order within 50px (400 blocks) of coastline
- Produces 111k river pixels — balanced between 98k (too few) and 178k (too many)

### River Edge Smoothing
- Gaussian blur (sigma=4.5, threshold 0.12) applied to centerline mask before EDT in `river_carver_v2.py`
- Smooths 8x8 NEAREST upscale staircases into organic curves

### River Water Fill in chunk_writer
- `build_column_array` now accepts `river_water_y` parameter
- `run_pipeline.py` saves pre-carve `surface_y`, computes `river_water_y = pre_carve_y - 1` for carved pixels
- Water fills from carved surface up to `river_water_y` for above-sea-level rivers

### River Depth Reduction
- `min_depths_by_order` defaults now `{1:1, 2:2, 3:2, 4:3, 5:4}` (was `{1:2, 2:3, 3:4, 4:5, 5:6}`)
- Max carve depth capped at 4 blocks via `max_carve_depth` config

### Seagrass in Rivers
- `chunk_writer` places seagrass/tall_seagrass on riverbed at ~25% density with mixed heights

### Bank Texture Overhaul
- Fine-scale opensimplex noise (scale=8, octaves=2) for bank dithering instead of coarse noise_b (scale=60)
- Mix: 55% mud, 25% coarse_dirt, 12% clay, 8% dirt

### Sugar Cane Removed from River Banks
- Despawned and caused entity lag — removed entirely from `_apply_river_banks`

### New Test World
- Vandirtest7, test tile (52, 53) for inland rivers
- TP: `/tp @s 26624 200 27136`

## SESSION 25 CHANGES (2026-03-29) — BIOME STUDIO, PALETTE EDITOR, NORTERRE PIPELINE

### Override Boundary Smoothing — 2-Stage Upscale Pipeline
- **Replaced** contour smoothing + full-res median with 2-stage approach:
  1. NEAREST 8192→16384, median filter (kernel=17) at 16384, NEAREST 16384→50k, light jitter (8 passes)
- Total pipeline time: ~4 minutes (was ~100 minutes with 50k median)
- Median filter eliminates staircase edges AND fills ocean phantom gaps
- Contour smoothing (B-spline) DISABLED — median at intermediate res is more effective

### Y-Axis Flip Fix
- `FLIP_Z` toggled to `False` — biome overlay now correctly oriented

### World Studio — New Features
- **Biome Studio (Tool D)**: Height×flow scatterplot, 7 draggable threshold lines, real-time recompute
- **Palette Editor**: Replaced view-only BiomePaletteWidget with full PaletteEditorWidget
  - Per-biome block stack table (QComboBox dropdowns), threshold sliders, 200×200 live preview
- **Alignment Panel**: Editable params (Flip Z, Scale, Smooth S, Jitter passes), Save + Rebuild buttons
- **Detachable Tabs**: Double-click any tab header to pop into floating window, close to re-dock
- **Refresh All** button: Flushes all caches + reloads overview
- **Spline Apply fix**: `_LUT` now rebuilt from thresholds.json on Apply (was hardcoded at module load)
- **Rebuild timeout**: Increased to 1800s (30 min)
- SimPreviewWorker now emits `flow_norm` for Biome Studio
- Tab max height increased from 280→400px

### Norterre-Inspired Pipeline Changes (column_generator.py)
1. **3-zone slope system**: <25° full biome surface, 25-50° mixed stone scatter, >50° full cliff
2. **Per-biome cliff stone variants**: 26 biome-specific cliff palettes (forest=mossy, desert=sandstone/terracotta, alpine=geological mix)
3. **Two-noise cliff banding**: Second noise octave for irregular horizontal layers
4. **Talus/scree generation**: Morphological dilation of cliff mask (8px), gravel/cobblestone scatter at cliff bases
5. **Shoreline gradient**: 3-band coastal transition (sand→gravel/clay→coarse_dirt) instead of single sand band
6. **Flow-path sand deposition**: Desert biomes place sand along drainage paths (flow 0.15-0.50)
7. **Snow cap slope exclusion**: Uses transition zone threshold (50°) instead of old cliff threshold

### Surface Palette Overhaul (surface_decorator.py)
Research-informed update of all 26 biome palettes:
- **moss_block** promoted to base surface in rainforest/lush biomes
- **rooted_dirt** subsurface throughout forest biomes
- **dirt_path** added to steppe/grassland/arid transition biomes
- **Terracotta banding** in DRY_OAK_SAVANNA, DRY_WOODLAND_MAQUIS, DESERT_STEPPE_TRANSITION
- **Granite subsurface** in CONTINENTAL_STEPPE
- **ARCTIC_TUNDRA** base changed from coarse_dirt to snow_block
- **BOREAL_TAIGA** base changed from grass_block to podzol
- New blocks in _BLOCK_COLORS: dirt_path, smooth_sandstone, dripstone_block

### Biome Map (world_biome_map.py)
- Removed assign_biomes() — reads override.tif directly via 256-entry LUT

### Validation
- Tile (56,46) all-land: 9 PASS, 0 FAIL
- Tile (50,46) coastal: 10 PASS, 0 FAIL

## SESSION 24 CHANGES (2026-03-28) — BIOME DISPLAY OVERHAUL

### CRITICAL: Override is sole biome source for ALL display
**Root cause of multi-session desert bug:** `assign_biomes()` procedural pipeline overwrote hand-painted zones with SAND_DUNE_DESERT. Three compounding bugs:
1. `world_biome_map.py` divided uint8 override by 65535 instead of 255 → all zone codes → 0
2. `BIOME_COLORS` RGB values differed between world_studio.py and world_biome_map.py → reverse lookup broke
3. `assign_biomes()` procedural stages overwrote painted zones for unoverridden pixels

**Fix:** Removed `assign_biomes()` from ALL display paths:
- `world_biome_map.png`: generated from `override_final_backup - Copy.png` → zone code LUT → colors
- `BiomeClusterLoader`: reads override.tif directly → LUT → colors + hillshade blend
- `SimPreviewWorker`: reads override.tif + height.tif directly → LUT → biome names/colors
- `BIOME_COLORS` synced between world_studio.py and world_biome_map.py (9409/9409 exact match)

### `tools/world_biome_map.py` — override normalization fix
- `read_tile_lowres`: override uses `Resampling.nearest` (was average) and `/255.0` (was `/65535.0`)

### `tools/world_studio.py` — zoomable biome layer
- `BiomeOverviewLoader` thread: loads `output/world_biome_map.png`, builds 97×97 biome-name grid
- `WorldMapView.set_biome_overview()`, `swap_layer()`, `_show_overview()`: swap background without resetting zoom
- "Biome" toolbar button now actually swaps overview layer
- Hover in biome mode shows biome name in status bar

### `tools/world_studio.py` — BiomeClusterLoader rewrite
- Removed `assign_biomes()` dependency — reads override.tif directly
- 256-entry RGB LUT from zone codes, nearest resampling, hillshade blend

### `tools/world_studio.py` — SimPreviewWorker rewrite
- Removed `assign_biomes()` dependency — reads override.tif + height.tif directly
- Emits `{h_norm, biome_rgb, biome_grid, override_8bit}` dict via LUT lookup

### `tools/world_studio.py` — BIOME_COLORS sync
- All 27 biome RGB values replaced to match world_biome_map.py exactly

### `tools/world_studio.py` — RiverExtractWorker rewrite
- Replaced raw flow percentile thresholding with riparian corridor detection
- Uses biome_assignment.py hydrology logic: `land & (height ≤ lowland_thr) & (flow ≥ flow_river_threshold)`
- No more false positives on ocean coastlines or ridgelines

### Override alignment fixes (from Session 22, finalized)
- tile_streamer.py: removed Y-flip special case for override reading
- upscale_override_vectorized.py: removed `np.fliplr()` — backup source is X-correct
- override_final.png + override_vectorized.png replaced with backup content

## SESSION 23 CHANGES (2026-03-28)

### `tools/world_studio.py` — Data Provenance Inspector
- `_terrain_class_at(h)` + `_provenance_at(h_norm, override_code, biome)` pure functions at module level — unit-tested
- `_OVERRIDE_NAMES` dict mirrors `OVERRIDE_BIOME_MAP` in biome_assignment.py
- `TileCanvas`: new `_h_norm_grid` + `_override_grid` fields; `set_provenance_data(h_norm, override_8bit)` method
- `SimPreviewWorker` now emits a **dict** (was tuple): `{h_norm, biome_rgb, biome_grid, override_8bit}`
- `_on_sim_done` unpacks dict, calls `set_provenance_data`
- Hover pill now shows 2-3 lines: biome name (bold amber), `raw · norm · terrain`, optional `override: 120 MIXED_FOREST`

### `tools/world_studio.py` — Hillshade/Sim Cache
- **h_norm in-session LRU cache** (`_h_norm_cache`, OrderedDict, 20 entries): revisiting same tile during session is instant
- **Biome sim disk cache** (`output/hillshade_cache/sim_{tx}_{tz}_{hash8}.pkl`): skips 3-5s biome sim on tile revisit if config unchanged
- Config hash covers biome-assignment keys only (terrain_class, hydrology, moisture, terrain_spline, etc.)
- `_from_cache` flag prevents double-write when loading from cache
- **"Clear Sim Cache"** button in Config panel — deletes all `sim_*.pkl` files

### `tools/world_studio.py` — Palette Preview tab
- `_BLOCK_COLORS` dict: 29 Minecraft block → approximate RGB values
- `_swatch_image(biome, w, h, palettes)` pure function: renders noise-textured swatch (top = surface, bottom = subsurface)
- `BiomePaletteWidget`: scrollable 3-column grid of swatches, auto-updates from sim_complete signal
- "Show All Biomes" toggle shows all 24 biomes for comparison
- New "Palette" tab added to right panel
- `_on_sim_complete` now calls `self._palette_widget.update_biomes(biome_names)`

### `tools/world_studio.py` — Spline Editor (Session 22)
- `SplineEditorWidget` class added (adapted from terrain_preview.py): draggable PCHIP spline, sea-level pin locked, Apply saves to thresholds.json
- New "Spline" tab in right panel

## SESSION 22 CHANGES (2026-03-27)

### `core/biome_assignment.py` — ocean override regression FIXED
- Root cause: source override PNGs have land zone codes (e.g. zone 120 = MIXED_FOREST) painted over ocean areas. With NEAREST upscale these are faithfully reproduced and applied at Stage 0, bypassing height-based ocean detection entirely.
- Fix: After connected-component cleanup, before Stage 0: `override_8bit[height_tile < sea_norm_thresh] = 0` where `sea_norm_thresh = 17050/65535 ≈ 0.260`. Ocean pixels always fall through to procedural assignment → `_OCEAN`.
- Validation: (48,48)→`_OCEAN` PASS, (56,46) land biomes PASS, (50,46) mixed PASS.

### `core/surface_decorator.py` — Phase 1 palette fixes
- CONTINENTAL_STEPPE: `andesite` → `granite` (sub-surface stone)
- DRY_OAK_SAVANNA: replaced coarse_dirt/gravel with `red_sand`/`terracotta`/`orange_terracotta` for laterite character

### Confirmed already done: histogram backing
- `MaskHistogramLoader`, `HistogramWidget`, `set_histogram_data`, and `_start_histogram_load` wiring were all already implemented. Histograms appear behind sliders on startup automatically.

## SESSION 21 CHANGES (2026-03-27)

### `run_pipeline.py` — float bug FIXED
- `generate_columns` was receiving float [0,1] height → `.astype(np.uint16)` → all zeros → all terrain at Y=-64
- Fix: `height_uint16 = np.round(masks["height"] * 65535.0).astype(np.uint16)` inserted before the call
- `run_pipeline.py` is now the correct generation path again

### `tools/world_studio.py` — Render-status overlay fully wired + granular stale marking
- `RenderManifest.set_sim/set_full` now accept `biome_names: list` parameter — stored in manifest entry
- `RenderManifest.mark_config_change` upgraded to granular logic:
  - `_global_hash`: hashes terrain_class/hydrology/moisture/sea_level/biome_patch/terrain_spline → change marks ALL tiles stale
  - `_surface_hash`: hashes all other non-biome-specific sections → change marks ALL tiles stale
  - `_sparse_hashes`: per-biome hash from sparse_overrides → change marks ONLY tiles containing that biome stale
- `PreviewPanel.sim_complete = pyqtSignal(int, int, list)` added — emits (tx, tz, biome_names) on successful sim
- `_on_sim_done` emits `sim_complete` with unique biome names (filtering `_OCEAN` etc.)
- `MainWindow._on_sim_complete(tx, tz, biome_names)` calls `manifest.set_sim` + `map_view.update()`
- `MainWindow._on_pipeline_done` passes `state["biomes"]` to `manifest.set_full`
- Result: every tile that gets a biome sim shows a **blue overlay**; every full MCA export shows **green**; any config change marks only genuinely affected tiles **amber**

## SESSION 20 CHANGES (2026-03-27)

### CRITICAL BUG FIXES

**1. `core/biome_assignment.py` — height polarity inversion fixed**
- Was using OLD inverted polarity: `t_height = 1.0 - height_norm`, `sea_norm ≈ 0.740`
- This caused ocean pixels (low raw values) to be classified as land and vice versa — biomes were completely wrong
- Fixed to CORRECT polarity (matches Session 13 CLAUDE.md): `t_height = height_norm`, `sea_norm = 17050/65535 ≈ 0.260`
- `land_range` corrected from ~0.260 to ~0.740 — all terrain class thresholds now correct
- Module docstring updated to document correct polarity
- Connected-component cleanup in `assign_biomes()`: labels each zone's components; any component < 400px flooded with dominant surrounding zone (8px dilation halo). Removes jitter-scatter blobs (single source px × 6× upscale = 36px) without eroding legitimate zone edges. MIN_ZONE_PX=400 (~11×11 source-pixels at 6× scale).

**2. `upscale_override_vectorized.py` + `masks/override.tif` — phantom biome fix**
- Root cause: `Image.BILINEAR` upscale (8192→50k) created intermediate pixel values (e.g. 50 between zone 0 and zone 100) that LUT-snapped to valid but wrong zone codes (ARCTIC_TUNDRA=50, RAINFOREST_COAST=70, etc.), creating ~6px-wide phantom biome bands at every zone boundary
- Fix: changed `Image.BILINEAR` → `Image.NEAREST` at line 172. Zone boundary organics come from the 3-pass jitter at source resolution, not from interpolation.
- `masks/override.tif` rebuilt — sanity check confirmed clean zone values, no phantom codes
- `CLAUDE.md` OVERRIDE RULES updated: rule 2 now says ALWAYS NEAREST (was wrongly "NEVER nearest-neighbour")

### WORLD STUDIO ADDITIONS (`tools/world_studio.py`)

**SimPreviewWorker fixes:**
- Progress signal now always updates status (was conditional on `_h_norm is None`)
- `_on_sim_done(None)` now sets status to "hillshade preview | biome sim failed (check console)" instead of silently returning
- Now emits 3-tuple `(h_norm, biome_rgb, biome_grid)` — grid stored for hover lookup

**Config panel:**
- Wrapped `ConfigPanel` in `QScrollArea` (fixes sliders being completely hidden — `setMaximumHeight(280)` was clipping the ~430px content)

**TileCanvas biome hover:**
- `set_biome_grid(grid)` — stores downsampled biome grid for hover
- `_img_xy(wx, wy)` — maps widget coords to image coords respecting pan/zoom
- `mouseMoveEvent` tracks hover pixel; `paintEvent` draws dark pill label with biome name near cursor
- `leaveEvent` clears hover state

**Cluster view (3×3 / 5×5 / 9×9):**
- `BiomeClusterLoader(QThread)` — reads masks at thumbnail size per tile, runs `assign_biomes`, blends 65% biome + 35% hillshade, emits QPixmap per tile
- `ClusterView` extended: variable radius (1=3×3, 2=5×5, 3=9×9), biome mode toggle, hover highlight
- Cluster controls toolbar: "3×3"/"5×5"/"9×9" checkable buttons + "Biome" toggle

**River layer:**
- `RiverExtractWorker(QThread)` — reads flow.tif at GRID_N resolution, thresholds at flow_pct percentile (default 85%), labels connected components, traces 8-connected paths, assigns Strahler-like order, emits `list[dict]`
- `RiverSketchStore.add` updated to accept `name`, `order`, `extracted` kwargs
- Map panel: "Extract Rivers" button + "Draw River" toggle added to header toolbar
- `drawForeground` renders rivers: order-scaled width/alpha, blue=extracted, cyan=manual

## SESSION 17 CHANGES
- Built `tools/world_studio.py` — the integrated single-pane tool (Tool A + C + E per ARCHITECTURE_VISION.md)
  - Tool A World Map: zoomable 97×97 tile grid over the full 50k world, height.tif overview, LOD tile thumbnails (rasterio on-demand per-tile), tile selection with amber highlight, grid overlay
  - Tool E Voxel Preview: WebGL2 height mesh (drag orbit/scroll zoom), cross-section with cliff banding reconstruction matching chunk_writer.py exactly
  - Tool C Tile Inspector: tile coords, world block range, MCA filename shown on selection
  - Pipeline steps 4-7 run in QThread; cross-section re-renders in QThread with 300ms debounce on slider drag
  - Controls: mode combo (3D/Cross-Section), Z-row slider, band_scale_y (4-48), cliff_deg_thr (10-80°)
  - Launch: `python tools/world_studio.py [--tile-x 56] [--tile-z 46]`
  - Also built `tools/voxel_preview.py` (single-tile quick viewer, superseded by world_studio)

## SESSION 16 CHANGES (part 2 — height fix)
- **Fixed `_TEST_SECTION_Y_MAX`**: was hardcoded to 15 (capping chunks at Y=255). Set to None — all 32 sections now written (Y=-64 to Y=447).
- **Installed `vandir_height.zip` datapack**: custom datapack with `min_y=-64, height=512` — exact match for Y_MIN/Y_MAX. Installed in Vandirtest5/datapacks/ and enabled in level.dat. The existing `HigherHeightsUltimate4064` datapack uses `min_y=-2032, height=4064` which is incompatible with our 9-bit heightmaps — do NOT use it.
- Tiles regenerated with full 32-section height range, all 3 PASS.

## SESSION 16 CHANGES (part 1 — geological detail)
- Built `tools/check_tile_seams.py` — tile boundary seam detector using process_tile_columns_v2
- Added cliff interior banding to `build_column_array` (biome-keyed stone: andesite/granite/diorite/tuff/sandstone)
- Added high alpine bare-rock exposure (Y≥340 with noise jitter) to process_tile_columns_v2 (step 6c)
- Added frost-shattered ridgeline scatter (Y≥300, cliff_deg≥35°) to process_tile_columns_v2 (step 6d)
- Added `geo_surface` section to thresholds.json (alpine_exposure_y=340, frost_ridge_y=300, frost_ridge_deg=35)
- Upgraded cliff_banding config: added `cliff_deg_thr=45.0` (was hardcoded 55°)
- Updated `write_tile` to accept and forward `cfg` for cliff banding params
- Updated `run_pipeline.py` and `validate_test_tile.py` write_tile calls to pass `cfg`
- All 3 test tiles re-generated with geological changes (9-10 PASS, 0 FAIL each)
- **KNOWN BUG**: `generate_columns` in run_pipeline.py is broken with float [0,1] input
  (returns all Y=-60). Pipeline only works via validate_test_tile.py path which uses
  process_tile_columns_v2 with uint16 input. To be fixed in Phase 2.

## SEAM CHECK RESULTS (Session 16, tiles 47-57 × 45-47)
- 52 seams checked, 2 above threshold=4:
  - Z-seam (49,46): max=11, mean=5.55 — ocean depth EDT artifact at coastal Z-boundary
  - Z-seam (57,46): max=6, mean=1.88 — minor
- All X-seams: max≤4 — excellent continuity
- Seam heatmap: `seam_report/seam_heatmap.png`

## OPEN ISSUES (priority order)
1. ~~Phase 3 rivers/lakes~~ — **DONE Session 26**: hydrology_precompute + river_carver_v2
2. ~~Ecological surface decoration~~ — **DONE Session 27**: eco_gradients + eco conditions in surface_decorator
3. ~~Wire layer stack palette into pipeline~~ — **DONE Session 27**: `_apply_noise_layers()` reads `noise_layers_biome` from thresholds.json, generates world-space seamless noise per layer, applies with palette editor coverage thresholds
4. ~~Sync eco conditions into validate_test_tile.py path~~ — superseded by run_pipeline.py being the primary generation path
5. ~~Three-layer alpine system~~ — **DONE Session 40**: alpine_meadow / rock / snow with biome inheritance
6. ~~Sand dunes mask~~ — **DONE Session 41**: gap==8 with terracotta+basalt rock palette
7. **Stratification rings issue (Session 41 followup)** — bands appear as concentric rings around hills in 3D ground view. Need new band axis (distance-from-ridge) or move to column_generator. **PRIORITY for next session.**
8. **Apply Physical Realism Layer pattern to other masks** — snow caps north accumulation, windthrow orientation, forest density by aspect, coastal beaches by flow. See `memory/feedback_physical_realism_layer.md`.
9. **Coastal beaches mask** — flat coastline near sea level, distance-to-ocean × slope gate × concavity
10. **Dune morphology mask** — anisotropic dune ridges within sand zones (deferred from Session 40)
11. **Tree schematic placement not generating trees in-game** — needs investigation
12. **Subsurface rock editor** — separate tool for vertical stratigraphy (see memory/project_subsurface_editor.md)
13. **Isometric preview tile** — 32×32 block patch with actual column_generator output
14. **Full 50k generation run** — hydro masks ready, eco pipeline wired, palettes tuned
15. **Schematic placement wiring** (minor — load_index() type mismatch)
16. Seam fix at (49,46) — deferred to post-50k-run

## ECOLOGICAL SURFACE DECORATOR (Session 27)
- **`core/eco_gradients.py`** (NEW): computes 8 ecological gradients per tile from existing masks
  - aspect, north_factor, concavity, soil_depth, moisture_index, wind_exposure, riparian_proximity, lake_fringe
  - ~37ms per 512x512 tile (3% of budget), gated EDT calls when no hydro masks
- **`core/surface_decorator.py`**: 6 eco condition tags added to BIOME_BLOCK_PALETTES
  - eco_moist, eco_dry, eco_ridge, eco_basin, eco_shallow_soil, eco_deep_soil
  - Sigmoid probability fields × noise modulation → stochastic boolean masks
  - Priority: base < eco_* < noise/moisture/erosion < altitude
  - Slope zones (3-zone Norterre + talus) now in production path via `_apply_slope_zones()`
  - Graduated riparian corridors (4 distance bands) using riparian_proximity + hydro_order
  - Ecological ground cover: canopy-driven density, species colony clustering (48px cells)
- **Backward compatible**: eco_grads=None falls back to pure noise-threshold behaviour
- **Config**: `eco_gradients`, `eco_vegetation`, `eco_ground_cover`, `eco_riparian` sections in thresholds.json
- **Palette sync**: all 27 biome palettes synced to user's noise_layers_biome edits; eco tags use only blocks present in user's edited palette for each biome

## KEY FILES
- `run_pipeline.py` — CLI entry point, ProcessPoolExecutor tile dispatch (imports river_carver_v2, eco_gradients)
- `upscale_override_vectorized.py` — builds masks/override.tif
- `config/thresholds.json` — all thresholds, source of truth (includes hydrology_engine + eco_* sections)
- `core/biome_assignment.py` — 4-stage biome logic (override → height/slope → hydrology → forest)
- `core/column_generator.py` — vertical block column generator
- `core/chunk_writer.py` — nbtlib .mca writer
- `core/eco_gradients.py` — **NEW Session 27** ecological gradient computation (aspect, concavity, soil depth, etc.)
- `core/hydrology_precompute.py` — **NEW Session 26** global river/lake extraction at 1:8
- `core/river_carver_v2.py` — **NEW Session 26** precomputed-mask river carver (replaces river_carver.py in pipeline)
- `core/river_carver.py` — legacy carver (still used by validate_test_tile.py)
- `core/tile_streamer.py` — reads all masks including 5 hydro masks
- `tools/world_studio.py` — integrated world studio (map, preview, spline, palette, hydro overlay)
- `tools/terrain_preview.py` — PyQt6 viewer (4 render modes, scroll/pan/zoom)
- `tools/override_aligner.py` — visual override alignment tool
- `tools/validate_test_tile.py` — single-tile validator
- `schematic_index.json` — schematic metadata (anchor_y, inset_depth, size)
- `PLACEMENT_VARIATION_SPEC.md` — Y-variation rules for schematic placement
- `PROJECT_BIBLE.md` — comprehensive reference (read this for full context)

## SLOPE THRESHOLDS (fixed Session 8)
`config/thresholds.json`: `steep = 0.65`, `very_steep = 0.35` (NOT 0.95/0.85 — those were wrong)

## WORLD GEOMETRY
- 50,000 × 50,000 blocks, tile 512×512, 97×97 = 9,409 tiles
- MC Y range: -64 (bedrock) to 448 (Higher Heights datapack)
- Sea level: Y 63
- Tile (48,48) = center test tile

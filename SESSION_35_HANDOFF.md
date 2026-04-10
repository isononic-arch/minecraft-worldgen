# Session 35 Handoff — Vandir World Generation Pipeline
**Date:** 2026-04-06  
**Previous session:** 34 (see `project_vandir_status.md` in memory)

---

## What Was Done This Session

### 1. RIVER BOUNDARY SMOOTHING — Major Architectural Change

**Problem:** Rivers had visible 8x8 block staircase edges from NEAREST upscaling the 1:8 hydrology masks to 50k. Lakes were smooth (terrain intersection at 50k) but rivers were blocky.

**Solution: Three-pronged approach to match lake boundary quality:**

#### A. Thin Channels → Spline Rasterization at 50k
- The meander pipeline already generates smooth cubic B-splines via `splprep`/`splev` in `hydrology_precompute.py`
- **New:** These spline parameters (`tck` tuples + width profiles) are now saved to `masks/river_splines.pkl` during `rebuild_centerline.py`
- **New:** `river_carver_v2.py` Section 4a loads splines, finds which ones intersect the current tile, evaluates `splev()` at 50k density, and rasterizes circles directly onto the 512x512 tile grid
- Result: smooth curves at target resolution — spline math goes directly to pixels, no intermediate grid

#### B. Braid Fill → Gaussian Smooth
- Braid fill zones (value 255 in hydro_centerline.tif) are wide solid areas
- NEAREST upscale creates 8x8 staircase on their boundary
- Gaussian filter (sigma=5 = half the 8px period) + threshold 0.5 smooths the boundary
- Safe because braids are wide enough that blur can't eat them

#### C. Lakes → Terrain Intersection (unchanged)
- `height_50k < spill_elevation` — already smooth

**Files modified:**
- `core/hydrology_precompute.py` — `_add_meander()` now returns `tck` when `return_tck=True`; `meander_rivers()` collects spline data + width profiles per branch and saves to `river_splines.pkl`
- `rebuild_centerline.py` — unchanged (calls `meander_rivers()` which now saves splines)
- `core/river_carver_v2.py` — Section 4a completely rewritten: loads splines, rasterizes at 50k per-tile; braid fill uses gaussian smooth instead of binary dilation

### 2. RIVER DEPTH OVERHAUL

- Min depths by Strahler order: `{1:3, 2:3, 3:4, 4:5, 5:6}`, max carve = 7
- Bowled parabola cross-section: `0.25 + 0.75 * (EDT_from_edge / 8)^0.5` — steep walls, flat deep center
- Flow-modulated depth: `0.8 + 0.4 * (flow / local_max)` — higher flow = 40% deeper (mimics how lakes get organic depth from terrain)
- Connectivity channels: minimum 3 blocks depth
- Bank leveling: 2px land border around rivers lowered to water surface Y (Section 7a)

### 3. VALIDATE_TEST_TILE.PY REWRITE

**Critical change:** The validate tool was using the LEGACY v1 river carver (`core.river_carver.carve_tile()`). None of the v2 features worked in the test tile pipeline.

**Fixed to match `run_pipeline.py`:**
- Now imports `core.river_carver_v2` and calls `carve_rivers()`
- Surface blocks generated via `decorate_surface()` (was extracting from pre-carve col_results — caused broken surface blocks)
- Added eco_gradients computation for ground cover
- Added water fill logic (river_water_y, lake flattening, connectivity fill, river-lake blend)
- Passes `river_water_y` to `write_tile()`

### 4. GROUND COVER OVERHAUL

**Problem:** Only 3 species at 9% coverage — sequential placement meant early species consumed all low-random-value pixels.

**Fix:** Replaced sequential placement with **weighted random selection** per pixel. Each candidate pixel picks ONE species via cumulative density thresholds from a second random draw.

- Now generates 15+ species at ~35% coverage
- `"bush"` (minecraft:bush) kept as-is — it IS a valid 1.21+ block
- Removed all `firefly_bush` entries globally
- `flowering_azalea` reduced to 0.003-0.005 (very rare)
- `sweet_berry_bush` reduced to 0.005 (damage concern)
- Flowers removed from MIXED_FOREST (no poppy/dandelion)
- `hanging_roots` replaced with `short_dry_grass` globally

### 5. SCHEMATIC FIXES

#### Tree Ground-Level Check
- `stamp_schematic()` underground check now uses the **placement center's** surface_y, not each individual block's per-pixel surface_y
- Fixes small pines being buried on sloped terrain (adjacent higher pixels culled their low leaves)

#### Fence Blockstate Preservation
- `_bare_name()` in `schematic_loader.py` now preserves `[blockstate]` from sponge .schem files
- `oak_fence[north=true,south=true]` flows through to chunk_writer's `_entry()` which parses it into NBT Properties

#### Fence/Gate Blockstate Rotation  
- When schematics are rotated (0/90/180/270°), directional blockstate properties now rotate too
- `north→east→south→west` for rot=1 (90° CW), applied to both directional keys (fence `north=true`) and facing values (gate `facing=west`)

#### Fence Gates Removed Globally
- All fence_gate variants mapped to `"air"` in `_SPONGE_BLOCK_REMAP` (sponge) and classic ID map
- Despite blockstate preservation + rotation, gates still rendered incorrectly in too many cases

#### Stripped Acacia → Dark Oak
- `stripped_acacia_log/wood` → `stripped_dark_oak_log/wood` in sponge loader remap
- Blockstate (e.g., `[axis=y]`) preserved through remap

### 6. OTHER FIXES
- Water buffer for tree placement increased 3→5px
- Test world path documented: `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\`

---

## Current State of Files

### Modified This Session:
| File | Changes |
|------|---------|
| `core/river_carver_v2.py` | Spline rasterization (4a), braid gaussian smooth, depth overhaul, bank leveling (7a), flow-modulated depth, junction blend |
| `core/hydrology_precompute.py` | `_add_meander()` returns tck; spline data export to pkl |
| `core/surface_decorator.py` | Weighted random ground cover, palette density tuning, bush→bush preserved, firefly_bush removed, hanging_roots→short_dry_grass |
| `core/chunk_writer.py` | Center-based underground check, blockstate rotation on schematic rotation |
| `core/schematic_loader.py` | Blockstate preservation in `_bare_name()`, fence_gate→air remap, stripped_acacia remap |
| `core/schematic_placement.py` | Water buffer 3→5px |
| `tools/validate_test_tile.py` | v2 carver, decorate_surface(), eco_grads, water fill logic |
| `run_pipeline.py` | No major changes (already used v2) |
| `diag_flow_threshold.py` | New — world-scale comparison diagnostic |

### Key Generated Files:
- `masks/river_splines.pkl` — 660 spline branches with tck + width profiles (must rebuild with `rebuild_centerline.py` if meander changes)
- `masks/hydro_centerline.tif` — 50k centerline (still used as corridor guide for order assignment + braid/wadi classification)

---

## Known Issues (End of Session 35)

### CRITICAL
1. **Connectivity channels still have water gaps near lake** — channel carves terrain but water doesn't fill the last few blocks before lake boundary. Needs investigation in carver Section 4d (Dijkstra path) + run_pipeline.py water fill logic. The `~lake_mask` exclusion in `river_channel` might cut off the overlap zone.

2. **Bare dirt surface check failing** — 12,822 pixels (5%) of bare dirt. From river bank areas where `decorate_surface()` doesn't assign a surface block. May need a post-carve fallback to assign dirt→grass_block on non-river land pixels.

### MEDIUM
3. **Some floating trees remain** — trees on small terrain features near water edges. The 5px water buffer helps but doesn't catch all cases. Could add a post-placement cull checking if tree footprint overlaps water.

4. **Fence connections still as posts** — blockstates ARE preserved from .schem files now, but MC fence connections are adjacency-dependent. If the schematic was built with adjacent solid blocks that don't exist in our world, the connection states from the schematic will be wrong. True fix: recompute fence connections based on the ACTUAL placed neighbors in the volume. Complex — deferred.

### LOWER
5. **Lake water Y steps** — visible in lake surface. Component labeling issue.
6. **Tile gen time** — ~40s for dry-run is fine, full gen is longer with schematic stamping.

---

## Next Steps (Priority Order)

### Immediate (Next Session Start)
1. **Deploy + in-game test** — Run `python tools/validate_test_tile.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x 51 --tile-z 53` then copy to Vandirtest10
2. **Connectivity channel water** — Debug Section 4d junction logic, check `~lake_mask` overlap, widen channel-lake connection zone

### Short Term
3. **Forest clearings / meadows / bare patches** — Full spec in `memory/project_clearings_spec.md`:
   - Three gap types: meadow (fire/grazing), windthrow (storm), bare (rocky/erosion)
   - Cellular noise gap mask in eco_gradients, suppress trees in gaps, shift ground cover
   - Per-biome frequency table defined (MIXED_FOREST 14%, ALPINE_MEADOW 12% bare only, etc.)
   - Alpine meadow = no meadow gaps (already IS meadow), only bare rocky patches

4. **Fence connection recompute** — After all schematics are stamped, scan the volume for fence blocks and recompute `north/south/east/west=true/false` based on actual adjacent blocks

### Medium Term
5. **Multi-tile generation run** — Generate a 3x3 or larger grid to test tile seams
6. **River-coast intersection** — smooth transition where rivers meet ocean
7. **Ecological distribution overhaul** — gradient-driven placement per Gemini framework

---

## Key Constants & Paths
- **Python:** System Python 3.14
- **Masks:** `C:\Users\nicho\minecraft-worldgen\masks\`
- **Test world:** `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\`
- **Lake TP:** `/tp @s 26606 200 27071`
- **Tile 51,53 TP:** `/tp @s 26368 200 27392`
- **DataVersion:** 4556 (MC 1.21.10)
- **Rebuild centerline:** `python rebuild_centerline.py` (~388s, regenerates hydro_centerline.tif + river_splines.pkl)
- **Generate test tile:** `python tools/validate_test_tile.py --config config/thresholds.json --masks masks/ --schem-index schematic_index.json --output output/ --tile-x 51 --tile-z 53`
- **Copy to test world:** `cp output/r.51.53.mca "C:/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region/r.51.53.mca"`

---

## Memory Files Updated This Session
- `project_vandir_status.md` — Full Session 35 changelog
- `project_river_pipeline_s35.md` — River architecture documentation
- `project_clearings_spec.md` — Forest clearings/meadows/bare patches spec
- `project_mc_version.md` — MC 1.21.10, block validity reference
- `feedback_test_world_path.md` — Modrinth path for Vandirtest10

# CLAUDE.md — Vandir World Generation Pipeline

*Auto-loaded by Claude Code. Lean operational doc. For strategy, history, and broad context, see `PROJECT_MEMORY.md`.*

**Current state:** Session 41 (2026-04-10) — sand dunes + desert rock palette landed. **NEXT:** stratification "rings around hills" fix.

---

## TOP PRIORITY (next session)

**Stratification rings bug.** `_apply_desert_rock_palette()` step 6 (`core/surface_decorator.py:1617`) uses absolute `surface_y` for the band index. Top-down it forms correct horizontal contours. In-game ground view it appears as concentric rings around conical hills.

Candidate fixes (pick one, prototype with `diag_layers_breakdown.py`):
1. Band axis = distance-from-ridge-crest along slope direction
2. Move stratification into `core/column_generator.py` so bands only show on cliff cross-sections
3. Accept top-down inaccuracy and drop stratification

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
**Current values:** `0`=none, `1`=meadow, `2`=windthrow, `4`=floodplain, `5`=rock, `6`=alpine_meadow, `7`=snow, `8`=sand_dune. (`3` is unused — bare patches removed.)

**Application order in `eco_gradients.py`** (each claims `gap==0` unless noted):
1. floodplain (4) → 2. alpine_meadow (6) → 3. rock (5) → 4. windthrow (2) → 5. meadow (1) → 6. **snow (7) — uses `land & ~water`, NO gap filter, overrides EVERYTHING** → 7. sand_dune (8) — overrides 0/1/2/4 only, NEVER 5/6/7.

**Final meadow override** (last pass in `decorate_surface`): dilates 2px, forces `grass_block` on gap **1 and 4 ONLY**. Never include 5/6/7/8 (re-creates staircases).

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

### Performance footguns
- **NEVER `binary_closing` with large structuring element** on 6250×6250. Use `binary_dilation(iterations=N) + binary_erosion(iterations=N)`.
- Vectorize labeled-component iteration with `np.bincount(labeled.ravel())`. No `for i in range(n_labels): mask == i`.
- `opensimplex.noise2array` at 1/4 res + bilinear upscale, not native 6250×6250.
- **Slope math gotcha**: `np.gradient(sy)` returns dY per ARRAY INDEX. At 1:8 that's 8× the real slope. `rebuild_sand_dunes.py` uses corrected `/SCALE`. `rebuild_rock_exposure/windthrow/floodplain` + `core/eco_gradients.py` use OLD inflated math with thresholds tuned to it — **don't touch without retuning**. `slope.tif` from Gaea is non-linearly normalized; don't assume `slope_norm * 90 = degrees`.

### Workflow
- **Render 3×3 top-down BEFORE generating .mca tiles.** Don't burn 18-min cycles blindly.
- **Always copy generated `.mca` to test world** immediately: `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\` (NOT `.minecraft/saves/`).
- **Failure logging**: after a failed fix, write timestamp + what + why to `memory/project_vandir_status.md` BEFORE retrying.
- **Stop looping**: after 2 failed fixes for the same symptom, STOP. Investigate data with a diagnostic, propose a different strategy, or ask the user.
- **Don't over-research.** 1-2 reference reads max, then write code.
- **Never suggest 50k runs.** Never suggest "save for next session." User decides.

---

## CURRENT PIPELINE STATE

All steps 1-15 complete. In-game validation passed (Session 15). Hydrology engine, river carver v2, ecological surface decoration, terrain-intersection lakes, three-layer alpine, sand dunes + desert rock palette all wired.

**Active masks (50k):**
- `override.tif` (NEAREST) — biome zones
- `height.tif` — terrain
- `flow.tif` — hydrology flow accumulation
- `slope.tif` — Gaea-normalized slope
- `hydro_centerline.tif` — Strahler NMS rivers + braid fill
- `hydro_floodplain.tif` (bilinear) — gap==4 corridors
- `hydro_lake.tif` + `hydro_lake_wl.tif` (float32 NEAREST) — lake basins + spill elevations
- `hydro_width.tif`, `hydro_depth.tif`, `hydro_lkdep.tif` — river/lake geometry
- `wind_windthrow.tif` (bilinear) — gap==2
- `rock_exposure.tif` (bilinear) — alpine gradient (gap==5,6)
- `rock_exposure_tight.tif`, `snow_caps.tif` — gap==7
- `sand_dunes.tif` (bilinear) — gap==8

**Rebuild scripts:** `rebuild_centerline.py`, `rebuild_floodplain.py`, `rebuild_windthrow.py`, `rebuild_rock_exposure.py`, `rebuild_sand_dunes.py`, `generate_lake_wl.py`.

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

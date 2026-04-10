# PROJECT_MEMORY.md — Vandir Strategic Context

*Broad project memory. Consolidates 40+ scattered memory entries into one widely-useful reference. Read this when CLAUDE.md doesn't have enough context. Updated after major milestones.*

**Last updated:** 2026-04-10 (Session 41)

---

## 1. WHAT VANDIR IS

A 50,000 × 50,000 block Minecraft Java world (1.21.10) generated from Gaea heightmaps via a custom Python pipeline. **Photoreal from a fixed-wing flight altitude.** Walkable, navigable, no human structures (no cities, no farms, no roads). The only constructed elements are biomes, hydrology, vegetation, and surface materials.

**Scale rules:**
- 1 block = 1 metre
- 50000 m = ~50 km — comparable to a small county
- 9409 tiles (97×97), each 512×512 blocks
- Tradewind direction: 270° (west → east)
- Sea level: MC Y 63 = Gaea raw 17050
- Vertical range: Y -64 to 447 (custom `vandir_height` datapack, height=512)

**Quality bar:** worth flying over at low altitude in spectator mode and seeing nothing that screams "procedural artifact." Tile seams, biome staircases, and noise blobs are the recurring enemies.

---

## 2. PIPELINE ARCHITECTURE

```
Gaea (external) → 16-bit PNG/EXR exports
   ↓
upscale_override_vectorized.py → masks/override.tif (NEAREST, painted zones)
core/hydrology_precompute.py → masks/hydro_*.tif (1:8 → 50k)
rebuild_*.py scripts → masks/{floodplain, windthrow, rock_exposure, sand_dunes, snow_caps}.tif
   ↓
run_pipeline.py (per tile, ProcessPoolExecutor)
   ├── core/tile_streamer.py — windowed reads from all masks
   ├── core/eco_gradients.py — 8 ecological gradients + gap_mask
   ├── core/biome_assignment.py — 4-stage biome logic (override → height/slope → hydro → forest)
   ├── core/river_carver_v2.py — terrain carving from precomputed centerline
   ├── core/column_generator.py — vertical block columns
   ├── core/surface_decorator.py — biome palettes + eco overlays + slope zones + ground cover
   └── core/chunk_writer.py — nbtlib .mca writer
   ↓
output/r.{x}.{z}.mca → copy to test world region/
```

**Performance shape:** ~80s/tile single-threaded, ~256MB peak per tile (BlockPalette + uint16 volume). 8GB RAM box, 4-thread parallel often OOMs. Mask rebuilds 2-8 min each.

---

## 3. THE GAP MASK SYSTEM

Surface variations are encoded as a `gap_mask` per tile, computed in `eco_gradients.py`. Each value triggers a different surface palette + ground cover ruleset in `surface_decorator.py`.

| Value | Name | Source | Notes |
|---|---|---|---|
| 0 | (none) | default | base biome palette |
| 1 | meadow clearing | wet basins, per-biome frequency | wildflowers, short grass |
| 2 | windthrow | TPI ridges + directional | exposed grass strips |
| 3 | (unused) | — | bare patches removed Session 38 |
| 4 | floodplain corridor | river-width modulated | gentle riparian |
| 5 | bare rock | above treeline + steep | probabilistic stone dither |
| 6 | alpine meadow | treeline transition | wildflower stone mix |
| 7 | snow cap | high elevation | overrides ALL gaps |
| 8 | sand dune | desert biomes | overrides 0/1/2/4 only |

**Application order matters.** See CLAUDE.md hard rules. Snow uses `land & ~water` and ignores existing gap values — it's the master override. Sand dunes go last but yield to alpine/rock/snow.

**Final meadow override** (last pass in `decorate_surface`): forces `grass_block` on gap 1+4 with 2px dilation. Only those two. Including 5/6/7/8 re-creates the staircases this whole system was built to avoid.

---

## 4. THE PHYSICAL REALISM LAYER PATTERN

Established Session 41. Standard for ALL future surface mask painting. Replaces noise-blob layering.

**Why:** noise-driven palettes were producing visible "blobs" and "spots" because noise has no physical meaning. Aspect, concavity, slope, and flow DO have physical meaning. Painted from physical signals, the world looks like it formed by erosion instead of decoration.

**Pattern:**
1. Pull primary signals from `eco_grads`: `aspect`, `north_factor`, `concavity_norm`, `wind_exposure`, `cliff_deg`, `slope`. Pull `flow_tile` from masks for water-driven features.
2. Use **hard thresholds** on physical signals to define layer regions.
3. Compose layer-by-layer. Each layer paints a **distinct block**. Two layers painting the same block double-counts.
4. The **decisive feature must be the LAST step** in the function. Anything painted earlier gets overwritten by later steps. (Hall-of-fame bug: basalt at step 5, stratification at step 6, ate 430/441 basalt pixels. Move basalt to step 7. Fixed.)
5. **Noise = ±10% edge jitter ONLY**, never the primary discriminator. It exists to break hard threshold lines, not to create features.
6. **Verify thresholds in the REAL pipeline**, not standalone test scripts. `concavity_norm` in particular has a much narrower distribution in the real pipeline than in standalone — `column_generator` and `river_carver_v2` smooth `surface_y` before decoration runs. A standalone test that hits 25% concave pixels will hit 0.4% in production.

**Reference implementation:** `_apply_desert_rock_palette()` at `core/surface_decorator.py:1617`. 7 layers: base orange_terracotta → south aspect (terracotta) → north aspect (brown_terracotta) → flow wash channels (smooth_sandstone) → stratification bands → subsurface fill → **basalt cap rock (last)**.

**Where to apply next:** snow cap north accumulation, windthrow orientation, forest density by aspect, coastal beaches by flow, riparian banks by flow magnitude.

**Where NOT to apply:** soft organic features like forest floor, moss patches, grass color variation. Noise IS appropriate there.

---

## 5. SOLVED PROBLEMS — INDEX

The "what we've already burned a session on" list. If a problem here recurs, START with the historical fix before exploring new ideas.

### Chunk writer / NBT
- **Biome PalettedContainer length error** (S15) — biomes need `min_bits=1`, not 4
- **MC hangs on ocean tiles** (S15) — fluid ticks must be top-water-only per column
- **SkyLight overflow** (S15) — omit light arrays entirely, set `isLightOn=0`
- **Tiles silently capped at Y=255** (S16) — `_TEST_SECTION_Y_MAX = None`
- **Double-tall plants invisible** (S30) — `[key=value]` must emit Properties NBT compound, not bake into Name string
- **Lakes/rivers freeze** (S30) — MC biome temp ≥ 0.5 except for the 3 snowy biomes

### Override / biomes
- **`run_pipeline.py` produced flat Y=-64 terrain** (S21) — float→uint16 height conversion bug, fixed in `run_pipeline.py`
- **Phantom 6px biome bands at zone boundaries** (S20) — `Image.BILINEAR` upscale snapping intermediate values to wrong zone codes; fix = NEAREST only
- **All-desert world bug** (S22-24) — three compounding bugs: world_biome_map dividing by 65535 instead of 255, BIOME_COLORS RGB drift between two files, `assign_biomes()` overwriting painted zones in display path. Final fix = display reads override.tif directly via LUT, never calls assign_biomes.
- **Ocean override regression** (S22) — source override PNGs had land zone codes painted over ocean; fix = post-cleanup ocean stamp `override[height < sea_norm] = 0`
- **Height polarity inversion** (S20) — biome_assignment was using inverted polarity; fixed: `t_height = height_norm`, `sea_norm = 17050/65535`

### Hydrology
- **Lake shorelines straight-line artifacts** (S30) — morph+blur on `hydro_lake` mask was tracing 8x8 NEAREST staircase. Fix: deleted all 2D smoothing. New approach: shoreline = `height < spill_elevation`, terrain naturally clips circular basins into organic shapes. Lake bank narrowed 6→2px, lake_fringe sources from `river_meta == CHAN_LAKE` not basin mask.
- **River tile-boundary seams** (S32-33) — per-tile NMS produced 24-147px mismatches at edges. Fix: precompute NMS + suppression + braid fill GLOBALLY at 1:8 in `nms_centerline()` → `hydro_centerline.tif`. Per-tile carver reads precomputed corridor. Zero seams by construction.
- **River meander naturalism** (S34) — edge noise and flow-warp displacement were too subtle. Only skeleton→control-points→cubic spline rebuild produced visible meanders. Amplitude order-scaled: o1=8 → o5=45.
- **River sugar cane spam** (S28) — entity lag, despawning. Removed entirely from bank palette.
- **River-lake water level step bug** (S28) — known issue at specific tile boundary, deferred.

### Surface / palettes
- **Clearings banding around contour lines** (S36) — `gap==1` meadow was dithering against biome palette and producing visible elevation contours. Fix: dilate 2px + final_meadow_override forces grass_block as last pass. Rule: never change surface noise type, only ratios within existing noise.
- **"Snow boundary staircase"** (S40) — was actually floodplain leaking through under snow's lower priority. Fix: snow uses `land & ~water` with no gap filter, applied last, overrides everything. (See `feedback_staircase_catalog.md` for 8 distinct staircase classes — check FIRST when seeing 8-pixel boundary lines.)
- **Hard rock/grass boundary above treeline** (S39-40) — replaced binary cutoff with probabilistic gradient dither. Extended ramp `(rock_exposure - 0.3) / 0.65` so grass pushes higher.
- **Windthrow blob → ridge-following** (S37-38) — switched from product to weighted SUM (0.45*TPI + 0.30*aspect + 0.15*elev + 0.10*slope), added directional anisotropy, target 6%.
- **Floodplain tile seams** (S37) — global 1:8 precompute, Strahler flood-fill, coast decay.
- **Three-layer alpine** (S40) — meadow / rock / snow stacked masks, biome inheritance via `EcoGradients.alpine_biome_source` (nearest non-alpine biome). No more hardcoded ALPINE_MEADOW.
- **Sand dunes** (S41) — gap==8, simplified SAND_DUNE_DESERT palette to single base block (8-11 noise layers were creating blobs), sand-flow proximity dither on adjacent rock pixels.
- **Desert rock palette** (S41) — terracotta + basalt cap rock via Physical Realism Layer pattern, 14.3% basalt coverage from concavity-driven volcanic dikes.
- **Treeline biome-shaped seams** (S41) — `gaussian_filter(treeline, sigma=15.0)` in `rebuild_rock_exposure.py` smooths over ~30 source pixels (240 blocks).
- **Schematic placement** (S34) — 9 hard-won rules (separate bush/tree passes, no leaves/fences underground, classic ID 6 → matching leaves, marker blocks → air, 3px river buffer, etc.).

### Tooling
- **`world_studio.py` integration** (S17-25) — single-pane tool: world map, voxel preview, cross-section, sim preview, biome cluster, hydro overlay, palette editor, spline editor.
- **Override boundary smoothing** (S25) — 2-stage NEAREST→16384→median(17)→NEAREST→50k pipeline, ~4 min vs 100 min for 50k median.

---

## 6. CRITICAL RULES (full ruleset, organized)

CLAUDE.md has the must-not-break version. This section adds the WHY for context.

### Mask generation
- **Gradient masks bilinear, discrete masks NEAREST.** Why: bilinear on zone codes creates phantom intermediate biomes. Bilinear on gradients creates smooth edges. The `> 0.001` threshold afterwards gives organic falloff.
- **Use weighted SUM, not product, for mask scoring.** Why: multiplicative composition crushes scores when any factor is low. Sum lets weak signals contribute. Session 39 lesson.
- **Smooth biome-driven values with gaussian** before propagating. Why: per-biome treeline Y differs by 100+ blocks; hard biome boundary → hard mask seam.
- **All `rebuild_*.py` scripts run at 1:8 (6250×6250).** Bilinear upscale to 50k via `write_upscaled()`. Register in `core/tile_streamer.py:MASK_NAMES`. Wire through `eco_gradients.py` (gap_mask) and/or `surface_decorator.py` (palette).

### Slope math footgun
- `np.gradient(sy)` returns dY per ARRAY INDEX. At 1:8 scale, that's 8× the real slope.
- `rebuild_sand_dunes.py` uses corrected `np.gradient(sy) / SCALE`.
- `rebuild_rock_exposure/windthrow/floodplain` + `core/eco_gradients.py` use the OLD inflated math with thresholds tuned to it. Don't fix it without retuning the thresholds — you'll silently break the pipeline.
- Aspect (direction, not magnitude) does NOT need the SCALE correction.
- Gaea's `slope.tif` is non-linearly normalized — `slope_norm * 90 ≠ degrees`. Use `eco_grads.cliff_deg` for actual angles.

### Performance
- **Never `binary_closing` with large structuring element on 6250×6250.** It runs for HOURS. Use `binary_dilation(iterations=N)` + `binary_erosion(iterations=N)` instead. Same shape, ~2 orders of magnitude faster.
- **Vectorize labeled-component iteration with `np.bincount(labeled.ravel())`.** Never `for i in range(n_labels): mask = labeled == i`.
- **`opensimplex.noise2array` at 1/4 res then bilinear upscale**, not native 6250×6250.
- `maximum_filter(size≤9)` is fine at 1:8. Size 41+ is slow.
- **dtype=object volume is a memory hog.** A `(512,512,512)` object array = ~4.8 GB per tile. Use BlockPalette + uint16 indices (~256 MB).
- **Read masks via `rasterio.Window()`.** Never full-load a 50k TIF.

### Code hygiene
- **No PyQt6/GUI imports in `core/` or `run_pipeline.py`.** GUI lives in `tools/`.
- **Always call `process_tile_columns_v2()`**, not `process_tile_columns()`. The v1 version is 21× slower.
- **Volume shape must be `(512, h, w)`.** Shape `(384, ...)` silently produces empty `.mca`.
- **chunk_writer.py uses nbtlib**, not amulet (amulet is not installed).

### Workflow
- **Top-down 3×3 validation BEFORE generating .mca.** Don't burn 18-min cycles blindly. Use `diag_*topdown.py` scripts.
- **Failure logging.** After any failed fix: timestamp + what + why + hypothesis in `memory/project_vandir_status.md`. Validate hypotheses with diagnostic data, not more code.
- **Stop looping.** 2+ failed fixes for same symptom → STOP. Investigate data, propose new strategy, or ask user. User has explicitly said "you are in a loop, stop" — don't make them say it again.
- **Don't over-research before coding.** 1-2 reference reads max, then write.
- **Update CLAUDE.md + this file at end of every session** or when user says "update memory."
- **Never suggest 50k runs.** Never suggest "save for next session." User decides scope and stopping points.

---

## 7. OPEN WORK / ROADMAP

### Next session priority
1. **Stratification rings fix** — see CLAUDE.md TOP PRIORITY.

### Apply Physical Realism Layer pattern to other masks
2. Snow cap north accumulation by `north_factor`
3. Windthrow orientation by aspect + wind direction
4. Forest density by aspect (south-facing drier)
5. Coastal beaches by flow + slope + concavity
6. Riparian banks by flow magnitude (currently uniform)

### New global masks
7. **Coastal beaches** — `rebuild_beach.py` → `beach_sand.tif`. Distance-to-ocean × slope gate × concavity. 5-15 block strip.
8. **Dune morphology** — `rebuild_dune_morph.py` → `dune_morph.tif`. Anisotropic noise along wind axis as height offset within sand zones. (Deferred from S40.)

### Hydrology cleanup
9. **Meander on connectivity extension channels** — `enforce_connectivity()` extensions currently don't get the spline meander treatment, only the main NMS rivers.
10. **River ocean cutoff verification** — confirm rivers actually terminate at coast and aren't generating inside ocean polygons.

### Quality polish
11. Brown terracotta currently 24.4% — could tighten threshold to 0.80 for more orange dominance in desert rock.
12. Basalt 14.3% — could cluster patches for more visual impact.
13. Subsurface basalt for cliff faces (requires `column_generator` integration — deferred).
14. Clean up inflated slope math in `rebuild_rock_exposure/windthrow/floodplain` (works but ugly).

### Tooling (from `tools/world_studio.py` gap analysis)
15. RenderManifest status overlays (sim/full/stale per tile)
16. Disk cache for hillshade
17. Biome override painting directly on map canvas
18. Annotation layer (notes/markers)
19. 3D fly-through (Panda3D or PyOpenGL)
20. Biome Studio (Tool D) — height×flow scatter with draggable thresholds (per ARCHITECTURE_VISION)
21. Subsurface rock editor — separate tool for vertical stratigraphy

### Deferred
22. Schematic placement final wiring (`load_index()` type mismatch — minor)
23. Tree schematic placement not generating in-game — needs investigation
24. Seam fix at (49,46) — Z-seam max=11, deferred to post-50k-run
25. Full 50k generation run — hydro masks ready, eco pipeline wired, palettes tuned. **User decides when.**

---

## 8. DIAGNOSTIC TOOLKIT

| Script | Purpose |
|---|---|
| `diag_layers_breakdown.py` | 12-panel layer breakdown for one tile (~45s) |
| `diag_sand_rock_world.py` | World-scale 1:8 preview of sand/rock/snow masks (~95s) |
| `diag_rock_staircase.py` | 6-panel rock boundary diagnostic |
| `diag_rock_surface_topdown.py` | Top-down rock palette result |
| `diag_floodplain_topdown.py` | 3×3 floodplain corridor visualization |
| `diag_river_3x3_topdown.py` | 7×3 river carver output |
| `diag_centerline_compare.py` | Red/blue/green NMS centerline compare |
| `diag_river_path.py` | Terrain-shaded world overview with terrain-intersection lakes |
| `tools/check_tile_seams.py` | Tile boundary seam detector |
| `tools/validate_test_tile.py` | Single-tile dry-run validator (8-10 PASS expected) |

---

## 9. EXTERNAL REFERENCES

- **Architecture vision:** `ARCHITECTURE_VISION.md` (in repo, destination state)
- **Mask quick-ref:** `MASK_PIPELINE_REFERENCE.md` (in repo)
- **Placement spec:** `PLACEMENT_VARIATION_SPEC.md` (in repo)
- **Vegetation spec:** `VEGETATION_MIX_SPEC.md` (in repo)
- **Auto-memory directory** (cross-conversation, NOT in repo): `C:\Users\nicho\.claude\projects\C--Users-nicho\memory\`
- **Git remote:** `https://github.com/isononic-arch/minecraft-worldgen` (identity `isononic-arch <isononic@gmail.com>`)
- **Test world:** `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\`
- **Python:** `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe`

---

## 10. SESSION LOG (compressed)

A one-line index of major milestones. Full session details previously lived in CLAUDE.md changelog and per-session memory files; the meaningful content has been distilled into Sections 5 and 6.

- **S1-14** Bootstrap → mask generation → block columns → MCA writer
- **S15** In-game validation passed (biome/fluid/spawn fixes)
- **S16** Geological detail (cliff banding, alpine exposure, frost ridges); height cap fix
- **S17** `tools/world_studio.py` built (Tool A+C+E integration)
- **S20** Critical fixes: height polarity in biome_assignment, override NEAREST upscale, world_studio sim preview wiring
- **S21** `run_pipeline.py` float bug fixed; render-status manifest
- **S22-24** Override=sole biome source overhaul; biome cluster view; BIOME_COLORS sync
- **S25** Biome Studio, palette editor, Norterre slope/cliff system, 2-stage override smoothing
- **S26** Hydrology engine, river carver v2, layer stack palette editor, hydro overlay
- **S27** Ecological surface decoration system (eco_gradients + 6 eco condition tags)
- **S28** River carver bug fixes, water fill, bank overhaul, OOM fix (BlockPalette)
- **S29** Lake seam fix (stochastic rounding, narrow bank taper)
- **S30** Lake terrain intersection (deleted 2D smoothing); NBT block state Properties fix
- **S32-33** Global NMS centerline precompute → zero river seams
- **S34** Schematic placement rules (in-game testing); river skeleton-to-spline meander
- **S35** River pipeline finalization (NMS at 50k, braid gaussian, terrain-following depth)
- **S36** Forest clearings (gap 1/2/3 spec); meadow override last-pass rule
- **S37** Floodplain corridors (gap==4); windthrow first wiring
- **S38** Rock exposure gradient; windthrow target 6% with weighted SUM; gap==3 removed
- **S39** Bilinear upscale standard for all binary masks; rock/grass probabilistic dither; gap==6 removed from final override
- **S40** Three-layer alpine system (meadow/rock/snow); alpine biome inheritance; staircase catalog
- **S41** Sand dunes (gap==8); desert rock palette (terracotta + basalt); Physical Realism Layer pattern as new standard; treeline gaussian smoothing. **Stop point: stratification rings.**

# PROJECT_MEMORY.md — Vandir Strategic Context

*Broad project memory. Consolidates 40+ scattered memory entries into one widely-useful reference. Read this when CLAUDE.md doesn't have enough context. Updated after major milestones.*

**Last updated:** 2026-04-14 (Session 54)

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
   ├── core/surface_decorator.py — biome palettes + eco overlays + ground cover
   ├── core/surface_pipeline.py — layer-based surface pass orchestrator (S44+)
   ├── core/layers/pass2_surface/ — slope-driven layers: cliff_face, talus_apron, grass_terrace, vertical_fluting, etc.
   └── core/chunk_writer.py — nbtlib .mca writer + geology subsurface fill (S47+)
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
| 8 | sand dune | desert biomes | overrides 0/1/2 only, NEVER 4/5/6/7 (S51 fix) |
| 9 | beach | EDT from ocean, Y=63 gate | claims gap==0 only (S51) |

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
- **Stone contour-line banding** (S49-S54) — stone/andesite/cobblestone bands traced every terrain contour line. ROOT CAUSE (S54): `run_pipeline.py` computed `cliff_deg` from raw `np.gradient(surface_y)` without smoothing; every 1-block staircase step read as 45°, firing `temperate_cliff_face` (≥35°) and `temperate_talus_apron` (18-35°). Fix: replaced with `compute_cliff_deg(surface_y)` (sigma=1.5 Gaussian pre-smooth). Sessions S49-S53 fixed secondary sources (noise_layers stone, eco overlay stone, slope zone assignments) that masked the primary cause. See `memory/CONTOUR_BANDING_FIX.md`.
- **River bank cobble/gravel** (S54) — cliff_face and talus_apron fired on carved river channel banks (2-4 block drops exceed thresholds even after smoothing). Fix: added `riparian_proximity >= 0.3` exclusion to both layers' scope masks.

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
- `np.gradient(sy)` returns dY per ARRAY INDEX. On integer terrain, every 1-block step reads as gradient=1.0 → 45°.
- **`run_pipeline.py` uses `compute_cliff_deg(surface_y)`** (Gaussian sigma=1.5 pre-smooth) since S54. This is the canonical cliff_deg for surface pipeline layers and eco_gradients. See `memory/CONTOUR_BANDING_FIX.md` for the multi-session hunt that led here.
- `rebuild_sand_dunes.py` uses corrected `np.gradient(sy) / SCALE` (1:8 scale correction).
- `rebuild_rock_exposure/windthrow/floodplain` use OLD inflated math with thresholds tuned to it. Don't fix it without retuning the thresholds.
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
1. **Ecotone dither width** (24px) — grass bleed at biome boundaries is too wide.
2. **Beach blob/staircasing** on tile (35,77).
3. **Desert pavement ground cover** — dead_bush/short_dry_grass/tall_dry_grass still pending.
4. **Stratification rings fix** — downgraded by user in S42 ("not that big"), likely subsumed by geology pass (CLAUDE.md DIRECTION #3).

### Resolved items (moved from next-session)
- ~~Riparian bare-dirt hole~~ STRICKEN S43 — validator false positive, not a real bug.
- ~~Snow cap north accumulation by `north_factor`~~ DONE S51 — precompute mask `snow_caps_north.tif`.
- ~~Coastal beaches~~ DONE S51 — `rebuild_beach.py` → `beach.tif`, gap==9, Y=63 constraint.
- ~~Stone contour-line banding~~ FIXED S54 — `compute_cliff_deg()` smoothing + riparian exclusion.

### Apply Physical Realism Layer pattern to remaining masks
2. Windthrow orientation by aspect + wind direction
3. Forest density by aspect (south-facing drier)
4. Riparian banks by flow magnitude (currently uniform)

### New global masks
5. **Dune morphology** — `rebuild_dune_morph.py` → `dune_morph.tif`. Anisotropic noise along wind axis as height offset within sand zones. (Deferred from S40.)

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
- **S42** Validator hardening: `_OCEAN` scope gates on `chk_no_bare_dirt_surface` + `chk_surface_block_variety`; memory strip in `validate_3x3.py` (null heavy buffers after per-tile checks, keep only stitch inputs) → 36% wall-time speedup on (48,48). Two 3×3 baselines committed to `tests/baselines/3x3/`: `48_48` (79 PASS / 0 FAIL, ocean/coast/seams) and `51_53` (74 PASS / 8 FAIL, floodplain/lakes/mixed forest, committed as baseline-with-known-failures). Validator surfaced a pre-existing **riparian bare-dirt hole** in MIXED_FOREST/TEMPERATE_RAINFOREST (~10-12% of river pixels → bare `dirt` on land); filed, parked, not a regression. Baseline-before-edit workflow rule added to CLAUDE.md. `CLAUDE.md` wall-time budget revised with per-tile reference data + `PYTHONUNBUFFERED=1` requirement.
- **S44** Physical-realism refactor kickoff. Phases 0 + 0.5 landed (`core/surface_pipeline/` package + empty-pass-list harness + `run_passes(layers, ctx)` API). Details in `PHYSICAL_REALISM_REFACTOR.md` §18.
- **S45** Phase 0.75 shadow-mode hookup landed (`run_passes([], ctx)` wired into `decorate_surface()` behind `config/thresholds.json → surface_pipeline.shadow_mode` flag + `VANDIR_SHADOW=1` env override, try/except-wrapped). 31 unit tests green; flag-OFF + flag-ON 3×3 runs clean on 48_48 / 51_53 — zero new PASS→FAIL flips. Session inserted §11 Phase 0.75 + session-kickoff pillar checkoff to prevent future phase-number drift.
- **S46** Phase 1.5 "Lithology wiring scaffolding" landed. Session began Phase 1 per handoff but codebase recon surfaced a **spec-vs-code architectural drift**: §11 Phase 1 targeted `column_generator.fill_column(...)` which does not exist; `column_generator` emits only a sparse `ColumnResult.blocks` dict that the .mca path never reads for mid-column fill. Real injection point is `core/chunk_writer.build_column_array()`. Surfaced to user per new Codebase-reconciliation rule; user picked "fatten the real path". §11 Phase 1 preserved verbatim but marked SUPERSEDED, new **Phase 1.5** subsection inserted. Scope: 4 new optional kwargs on `build_column_array()` (`lithology_tile`, `sediment_thickness_tile`, `soil_horizon_depth_tile`, `use_new_geology`) + pass-through through `process_tile_columns_v2` / `generate_columns`; early-out guard makes flag-OFF byte-identical to S45; flag-ON raises `NotImplementedError` deferred to S47; 5 new unit tests. Tile choice corrected from 36_20 (desert, wrong) to **59_53** (windthrow / temperate mountain reference) as new 3×3 baseline. New §19 Spec-vs-Code Drift Log started in `PHYSICAL_REALISM_REFACTOR.md`; CLAUDE.md session-kickoff pillar checkoff extended with step 5 "Codebase reconciliation check". **Stop point: Phase 1.5 complete; Phase 2 (real geology content in flag-ON branch) next.**
- **S47** Phase 1.75 "Real geology content" landed + flag flipped ON + surface decorator gating + visual tuning. Geology sublayers 1-4 working in-game: deepslate bedrock band, lithology-palette basement banding (randomised 4-10 block thickness, XZ waviness, ±3Y per-column noise), flow-driven sediment, slope-driven soil horizon. Surface decorator gated: gap==5 rock uses soil palette (grass/coarse_dirt/gravel/packed_mud) instead of legacy terracotta/basalt mesh when geology ON. Found + fixed lithology read coordinate bug (reading at full-res offsets from 1:8 mask → all zeros). Palettes expanded to 6-8 blocks per group. In-game verified on 24_80 (124-block cliffs, sedimentary+deepslate contact) and 59_53 (temperate rainforest). 46/46 unit tests. **Stop point: all-biome surface gating next.**
- **S48** Phase 2.0 — Temperate Mountain pilot layers: `temperate_cliff_face` (≥35° lithology-keyed rock), `temperate_talus_apron` (18-35° cobblestone/gravel), `vertical_fluting` (overlay: columnar weathering stripes on claimed cliffs). Surface pipeline wired into `decorate_surface()` behind feature flag.
- **S49** Biome consolidation, ground cover gating, cliff_deg staircase aliasing fix (`compute_cliff_deg` with Gaussian smoothing added to `eco_gradients.py`). Stone banding partially reduced but not eliminated.
- **S50** Phase 2.75 partial — 3 new layers (`river_bar`, `desert_pavement`, `beach_surface`), `ForestSurface` removed (legacy decorator handles forest floor). Beach deferred to precompute mask approach.
- **S51** Snow cap north + beach as precompute masks. `snow_caps_north.tif` via `rebuild_rock_exposure.py`, `beach.tif` via `rebuild_beach.py`. Gap==9 (beach) added with Y=63 constraint. Sand dune gap fixed to never override floodplain/rock/alpine/snow. 24_80 baseline snapshotted.
- **S52+S53** GrassTerrace biome-aware scatter, eco overlay restored with organic-only surface blocks (stone→packed_mud etc.), legacy slope zone deletion in column_generator, noise_layers stone purge (7 biomes). Stone banding still visible in-game.
- **S54** **Stone contour-line banding ROOT CAUSE + river bank fix.** (1) `run_pipeline.py` computed `cliff_deg` from raw `np.gradient(surface_y)` — every 1-block step = 45°, firing cliff/talus layers at every contour line. Fixed: replaced with `compute_cliff_deg()` (sigma=1.5 Gaussian). (2) River banks showed cobble/gravel because carved channels exceed thresholds even after smoothing. Fixed: `riparian_proximity >= 0.3` exclusion on both layers. Both validated in-game on (51,53). See `memory/CONTOUR_BANDING_FIX.md`.

---

## 11. SCALE CHALLENGES — tiered by severity

*Added 2026-04-10 during cleanup/revamp pass. Living list of "things that will bite us as Vandir grows." Reorder as priorities shift.*

Severity key:
- **P0 — Blocker.** Will prevent or corrupt a full-scale run if not addressed. Fix before anything downstream of it.
- **P1 — High.** Makes iteration painful or unreliable. Eats days of wall time over the life of the project.
- **P2 — Medium.** Quality/polish cost. Visible but tolerable.
- **P3 — Low.** Nit or deferred niceness.

### P0 — Blockers

1. ~~**Iteration wall time: 5-20 min per full .mca tile.**~~ **LARGELY RESOLVED (S41-42).** `tools/validate_3x3.py` runs pre-MCA only (no .mca write, no schematic placement) via shared `_pipeline_runner.py`. Measured budgets: deep ocean 3×3 ~10 min, land-heavy 3×3 ~20-60 min. Memory strip in S42 dropped land runs ~36%. Delta mode (`--affects-key`) wired. Remaining: land-heavy 3×3 is still 20-60 min wall time — acceptable for iteration but not snappy. Further gains would require `np.memmap` on hydro masks or cropping masks once per 3×3 window instead of per-tile (both `core/` changes).

2. **`_TEST_SECTION_Y_MAX` silent drop history.** CLAUDE.md enforces `None`, but no check asserts sections above Y=255 actually survived in a written .mca. A regression here produces silently truncated tiles only caught by in-game inspection. Needs: lightweight nbtlib chunk parser in the validator asserting `len(sections) >= expected_count`.

3. ~~**No regression baseline.**~~ **PARTIALLY RESOLVED (S42).** `tests/baselines/3x3/` now holds committed baselines. `48_48` (ocean/coast/seams, clean) and `51_53` (rivers/lakes/mixed forest, committed with 8 known pre-existing FAILs — still regression-useful because `--baseline` only flags new PASS→FAIL flips). Workflow rule in CLAUDE.md: snapshot a baseline *immediately before* editing a `core/` path not already covered, then diff after. Remaining gap: tiles `24_80`, `36_20`, `59_53`, `16_73`, `25_72` still uncovered — add on-demand when their code paths are about to be touched.

### P1 — High (slow iteration, unreliable signal)

4. **Single-threaded mask rebuilds.** 2-8 min × 6 rebuild scripts = 20-40 min to refresh all masks after one config edit. Some are independent (sand_dunes vs rock_exposure) and could run in parallel. Needs: investigate shared-memory cost vs win.

5. **Stratification rings bug (downgraded S42, likely subsumed by geology pass).** Localized cosmetic in desert regions. CLAUDE.md DIRECTION #3 notes the geology pass likely subsumes this. P1→P2 pending geology completion.

6. **Inflated slope math in `rebuild_rock_exposure/windthrow/floodplain`.** These rebuild scripts still use the old inflated `np.gradient` math with thresholds tuned to it. `run_pipeline.py` and `eco_gradients.py` now use smoothed `compute_cliff_deg()` (S54 fix). The rebuild scripts are independent — they don't share thresholds with the surface pipeline — but the inconsistency is a landmine. Needs: explicit migration plan + coordinated retune in one session.

7. ~~**No mask sanity validator.**~~ **RESOLVED (S41-42).** `tools/validate_masks.py` checks dtype + shape + coverage % bounds per mask from `config/validation_affects.json`. `tools/validate_3x3.py` runs 9 tiles through pre-MCA pipeline with per-tile + seam checks, supports baseline-diff (`--baseline`) for regression detection, and delta mode (`--affects-key`) to skip unaffected checks. Two 3×3 baselines committed: `tests/baselines/3x3/48_48` (ocean) and `51_53` (land, known failures). Remaining gap: per-zone distribution diffs — a regression that scrambles zone mix but keeps total coverage in-band will still pass. Filed as TODO against the baseline system.

8. **Memory ceiling.** 8 GB + 256 MB/tile + GUI + rasterio windows + schematic palette = 4-thread parallel OOMs. Any full run at 4 threads is memory-bound, not CPU-bound. Needs: per-tile peak memory profile + reduction targets.

### P2 — Medium

9. **River ocean cutoff.** §7.10. Rivers may still generate inside ocean polygons at certain coast profiles.

10. **Meander on connectivity extensions.** §7.9. Extension channels don't get the spline meander treatment. Visual inconsistency at river forks.

11. **Brown terracotta 24.4% in desert rock.** §7.11. Tighten to 0.80 for more orange dominance. Aesthetic.

12. **Basalt 14.3% could cluster.** §7.12. Aesthetic.

13. **Seam at (49,46) Z-seam max=11.** §7.24. Known, deferred.

### P3 — Low

14. Subsurface basalt for cliff faces (§7.13) — requires `column_generator` integration.

15. `diag_*.py` proliferation. 35 scripts at root, most session-scoped. Being resolved by the 2026-04-10 cleanup pass.

16. Schematic placement wiring deferred items (§7.22, §7.23).

---

*The point of this list: by the time we're near any full-scale run, every P0 is empty.*

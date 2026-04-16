# CLAUDE.md — Vandir World Generation Pipeline

*Auto-loaded by Claude Code. Lean operational doc. For strategy, history, and broad context, see `PROJECT_MEMORY.md`. For the physical-realism refactor plan + implementation log, see `PHYSICAL_REALISM_REFACTOR.md` (§18 is the running log).*

**Current state:** Session 57 (2026-04-16) — **Phase 2.75e + Phase 3a complete.** Phase 2.75e: eco overlay loop disabled (no more banding), GrassTerrace archived, zone 40 MC biome inheritance fixed, grass cutoff at elevation (Y 325→350), packed_ice purged. Phase 3a: `meadow_clearing_field` wired into pipeline; clearings = solid grass_block interior + **gradient salt-and-pepper edge softening** (per-pixel decision coin `0xC1EA5F`, same seed for surface + ground cover → aligned pixels). Ground cover 77% short_grass interior / 36% tall_grass at seam; flowers ≈1/500 interior (spec-compliant). Trees fully suppressed in clearing interior, ×0.4 density in seam. Floodplain gap==4 edges get same 8-block EDT softening. Boreal moss scales with concavity_norm; moss excluded from snow biomes (blocks snow_layer accumulation). Forest biome `noise_layers_biome` rebalanced (6 biomes): grass_block base → podzol/coarse_dirt/moss_block base, grass sparse overlay (5-12%). **Critical fix:** `"gaussian"` noise type was misnamed fBm simplex producing blob patterns — added real `"white"` noise type for true per-pixel salt-and-pepper; all 6 forest biome overlays switched. New `NOISE_PATTERNS.md` at project root documents 5 noise/dither patterns with classification rules — **READ BEFORE any noise/random/probability code.** **Carry-forward:** Phase 3b cross-tile ecotone (user's top priority), schematic placement end-to-end verification (0 placements on (51,53) validator), rename misleading `"gaussian"` noise type config-wide, ground cover ecotone, rock lithology palette softening, SEMI_ARID_SHRUBLAND sand patches, desert_pavement ground cover, Phase B height.tif regen from Erosion2_Out, aspect convention drift, world_studio.py duplicate BIOME_TO_MC, snow tuning.

---

## DIRECTION (active)

**Physical realism for surface + subsurface geology.** Goal: stop painting blocks from biome-window noise lookups and start deriving them from physical signals end-to-end, top of column to bedrock.

Working backlog (Claude picks order, user vetoes):

1. **Surface block selection from physical drivers** — extend the S41 Physical Realism Layer pattern to every "soft" mask still using noise-as-discriminator. Candidates: snow cap north-factor, windthrow aspect, forest-floor density by aspect, coastal beach width by wave fetch, riparian banks by flow magnitude, moss/leaf-litter by humidity+canopy. Noise stays as ±10% edge jitter only.
2. **Subsurface geology pass** — `core/column_generator.py` currently fills below-surface with a thin uniform stack. Replace with a real lithology model: bedrock band, basement rock by elevation/region, sediment thickness driven by `concavity_norm` + flow accumulation, soil horizon thickness by biome+slope. Should make cliff cross-sections and river cuts read as real geology in-game.
3. **Stratification rings bug** (parked, lower priority). `_apply_desert_rock_palette()` step 6 (`core/surface_decorator.py:1617`) uses absolute `surface_y` so bands appear as concentric rings around conical hills in ground view. Fix candidates: band axis = distance-from-ridge along slope, OR move stratification into `column_generator.py` so bands only show on cliff cross-sections. The geology pass (#2) likely subsumes this.

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

# S70 Handoff — pick up here next session

**Status: 17 plan items + 5 follow-up patches all shipped, uncommitted.  Walk validation 80% complete.  Final 22-tile + 4 ocean-mistake-replacement chain pending.**

## Tldr for next session
1. **First action:** ask user how the latest SAND_DUNE_DESERT render at tile (18,66) walked.  That tile shipped 17:50 CDT 2026-04-26 with the `_NON_PLANTABLE` sand-fix + `eco_density_mod=0.20` + `cactus` species multiplier.  If user confirms vegetation is now visible, all S70 fixes are validated and we can fire the final chain.
2. **Then:** render the remaining 22 biome reference tiles + 4 ocean-mistake new tiles in one autonomous chain.  Estimated 4-5 hr wall.  Tile list in `memory/biome_reference_tiles.csv`.
3. **Then:** user does final walkthrough; mark up `memory/BIOME_VALIDATOR_CHECKLIST.md`.
4. **Then:** commit S70 to master.

## Files modified this session (uncommitted)

### Code (core/)
| File | What changed |
|---|---|
| `core/chunk_writer.py` | A: BIOME_TO_MC ARCTIC_TUNDRA→snowy_plains, DRY_WOODLAND_MAQUIS→wooded_badlands.  Plan B trunk threshold: `runs >= max(TRUNK_RUN_MIN, max_run * TRUNK_RUN_FRAC)` with `TRUNK_RUN_FRAC=0.85`.  All session-added water-overhang / canopy-anchor code REVERTED. |
| `core/eco_gradients.py` | C: floodplain skip in RIPARIAN_WOODLAND, FRESHWATER_FEN, LUSH_RAINFOREST_COAST, SAND_DUNE_DESERT.  D: sand-dune strict gate (no fall-through when biome_grid is None). |
| `core/surface_decorator.py` | E: meadow override exception same 4 biomes.  F: `_SAND_DUNE_SPECIES` bumped (bush 0.5, dead_bush 0.6, short_dry_grass 0.6, tall_dry_grass 0.5, **cactus 0.7**).  G: GROUND_COVER_PALETTES tweaks (COASTAL_HEATH flowers /5, DRY_OAK flowers /2, KARST short_grass↑/dry_grass↓, FROZEN_FLATS new, ARCTIC_TUNDRA scrubland).  H: RAINFOREST_COAST mud reduced 3→1 surface entries.  **`eco_density_mod[sand_dune_px] = 0.20`** (was 0.05).  **`_NON_PLANTABLE` set: sand/red_sand/suspicious_sand REMOVED** (was killing all dune vegetation). |
| `core/schematic_placement.py` | I: tree densities — CONTINENTAL_STEPPE 0.06→0.005, ARCTIC_TUNDRA 0.015→0.005, BOREAL_TAIGA 0.22→0.40, SBT 0.12→0.22, BOREAL_ALPINE new 0.16, COASTAL_HEATH 0.05→0.10, FROZEN_FLATS 0.0→0.04, KARST_BARRENS 0.20→0.35, SAND_DUNE_DESERT 0.008→0.020, LUSH 0.26→0.36.  FROZEN_FLATS removed from NO_BUSH_BIOMES.  M: palm distance gate (LUSH_RAINFOREST_COAST: rfpalm/mpalm/cpalm only fire within 32 blocks of water).  N: KARST clustering simplex modulator [0.0, 2.5] range. |
| `core/river_carver_v2.py` | P: water_y spline smoothing — 5×5 minimum_filter on river_channel pre-carve to flatten water surface across channel width (fixes the hillside-tilt bug from "River challenge.png"). |
| `core/region_overlay_smoothing.py` (new) | Median + jitter helper for paint regions.  Used by build_lithology + clean_painted_river_mask. |
| `core/hydro_region_overlay.py` | Wired `clean_painted_river_mask` (opening + endpoint prune) before bresenham rasterization. |
| `core/layers/pass2_surface/river_bar.py` | SAND_DUNE_DESERT removed from `ARID_BIOMES` (no more wadi-bar streaks in dunes). |

### Tools / scripts
| File | What changed |
|---|---|
| `tools/world_studio.py` | Q: replaced local BIOME_TO_MC dict with `from core.chunk_writer import BIOME_TO_MC` (sync). |
| `tools/build_lithology.py` | Wired `smooth_region_paint` before 8192→6250 NEAREST decimate. |
| `tools/diag_world_map_comprehensive.py` (new) | World map renderer with hillshade, lithology overlay, halo rivers, organic painted rivers via skeletonize+bresenham. Env toggles: `MAP_NO_PAINTED_RIVERS=1`, `MAP_IN_GAME=1`. |
| `tools/diag_biome_only_map.py` (new) | Pure-biome map for inspecting override.tif content. |

### Config + masks
| File | What changed |
|---|---|
| `upscale_override_vectorized.py` | Added Phase 4.5 (ocean-Y prune to 0) + Phase 4.6 (coastal-zone repaint, 32-block band, distance-transform fill).  `MEDIAN_KERNEL = 9` (was 17).  `ALIGN_SCALE = 1.00` (was 1.01 — caused painted features to drift). |
| `override_vectorized.png` | Replaced with copy of `override_final.png` (S70 workaround for stale-composite bug).  `.bak_s70_stale` retained. |
| `masks/override.tif` | Re-upscaled with all fixes (Apr 26 17:50). 227M ocean pixels cleared, ~1.7M coastal pixels repainted. |
| `masks/lithology.tif` | Rebuilt with smoothing helper applied (Apr 24 17:12). |
| `memory/biome_reference_tiles.csv` | Refreshed via `tools/diag_biome_sampler.py` against pruned override (4 ocean-mistake biomes now have land tiles). |
| `memory/BIOME_VALIDATOR_CHECKLIST.md` | Same. |

## Tile-status table

These tiles are in `Vandirtest10/region/`.  The "render" timestamp is the SHIP timestamp; "tested" is whether user has walked it post-shipping.

| Biome | Tile | Render | Last-fix iteration | Tested? |
|---|---|---|---|---|
| KARST_BARRENS | (34,9) | 21:02 Apr 26 | f4 (clustering [0.0,2.5]) | ✅ user said good |
| LUSH_RAINFOREST_COAST | (6,68) | 14:45 Apr 26 | f4 (no flood, palm-shore) | ✅ user said good |
| RIPARIAN_WOODLAND | (80,50) | (earlier) | base (flood-skip) | ✅ user said good |
| MIXED_FOREST | (50,50) | (earlier base) | base | (untested with all fixes) |
| FRESHWATER_FEN | (8,73) | (earlier) | base (flood-skip) | (untested with all fixes) |
| BOREAL_ALPINE | (27,9) | (earlier) | base | (untested) |
| BOREAL_TAIGA | (59,44) | (earlier) | base | (untested with new density) |
| SNOWY_BOREAL_TAIGA | (27,13) | (earlier) | base | (untested with new density) |
| ARCTIC_TUNDRA | (32,13) | (earlier) | base (snowy_plains MC) | ✅ user said scrubland good but high-altitude veg-cutoff observation |
| FROZEN_FLATS | (33,6) | (earlier) | base | (untested with new GC palette) |
| TEMPERATE_DECIDUOUS | (32,31) | (earlier) | base | (untested) |
| RAINFOREST_COAST | (13,82) | (earlier) | base (no palms, less mud) | (untested with all fixes) |
| DRY_OAK_SAVANNA | (29,76) | 17:25 Apr 26 | f5b (TRUNK_RUN_FRAC=0.85) | ⚠ "appendages still" — partially fixed |
| BIRCH_FOREST | (60,41) | (earlier) | base | (untested) |
| EASTERN_TEMPERATE_COAST | (28,35) | NOT RENDERED | new ocean-mistake replacement | (need render) |
| TIDAL_JUNGLE_FRINGE | (31,89) | NOT RENDERED | new ocean-mistake replacement | (need render) |
| SCRUBBY_HEATHLAND | (85,79) | NOT RENDERED | new ocean-mistake replacement | (need render) |
| SEMI_ARID_SHRUBLAND | (27,65) | NOT RENDERED | new ocean-mistake replacement | (need render) |
| CONTINENTAL_STEPPE | (39,23) | (earlier) | base (density 0.005) | (untested with new density) |
| DRY_PINE_BARRENS | (30,49) | (earlier) | base | (untested) |
| DESERT_STEPPE_TRANSITION | (19,63) | NOT RE-RENDERED with f5b | (need render) |
| DRY_WOODLAND_MAQUIS | (30,90) | (earlier) | base (wooded_badlands MC) | (untested) |
| SAND_DUNE_DESERT | (18,66) | **17:50 Apr 26** | f5b (sand plantable + 0.20 eco_dm + cactus) | **PENDING WALK** |
| MANGROVE_COAST | (30,86) | (earlier) | base | (untested with all fixes) |
| RIPARIAN_WOODLAND_2 | (73,53) | (S70 supplement) | base | ✅ |
| FRESHWATER_FEN_2 | (8,74) | (S70 supplement) | base | ✅ |
| COASTAL_HEATH | (37,8) | (earlier) | base (flowers /5) | (untested) |
| TEMPERATE_RAINFOREST | (23,29) | (earlier) | base | (untested) |

## Open issues / carry-forward

### Highest priority
1. **SAND_DUNE_DESERT walk verification** — most recent render at 17:50.  User report needed.
2. **Final 22-tile + 4 ocean-replacement chain** — ~5 hr autonomous render.  Use `memory/biome_reference_tiles.csv`.

### Schematic-level (per-tile)
3. **DRY biome small trees still bushy on `_b_sm` and `_f_lg` schemas** — Plan B at 0.85 trunk_run_frac may not be enough.  User suggested editing the .schem files in schem_viewer to remove leaves at low sy in non-trunk columns.  See `Vegetation/dosav_tree_soak_b_sm.schem` (also similar in dpine, dstep, maquis).
4. **MANGROVE_COAST schematics floating roots** — user will fix in schem editor.
5. **Sand_dune cross-biome contamination** at tile (24,53) — covered partly by Item D's strict gate; verify.

### Pipeline-level
6. **Override Studio Save→upscale workflow** — Save (Ctrl+S) writes `override_final.png` only.  Save+Upscale (Ctrl+Shift+S) regenerates `override.tif`.  Plus stale-vectorized bug now requires the `cp override_final.png override_vectorized.png` workaround.  Should be fixed permanently in `upscale_override_vectorized.py` to read `override_final.png` directly, OR in `tools/override_studio.py` to make Save always trigger upscale.
7. **`hydro_centerline.tif` value 128/255 docs** — undocumented in CLAUDE.md MASK_PIPELINE_REFERENCE.md.  128 = wadi/dry channel, 255 = braid fill.  World map missed these initially.
8. **River-water-tilt fix (Item P)** — committed in this session but **not yet validated in-world**.  Walk a hilly-river tile (e.g. ARCTIC_TUNDRA at 32,13 has the screenshot bug) to verify spline-smoothed water_y.
9. **River-delta-doesn't-connect-to-ocean** in TEMPERATE_DECIDUOUS, DRY_WOODLAND_MAQUIS — separate from water-tilt; hold until P is verified.

### Map / tooling
10. **Biome-only diagnostic map** (`tools/diag_biome_only_map.py`) ready for use whenever override changes — auto-pulls latest, mode-pool downsample for crisp boundaries.
11. **World map env toggles**: `MAP_NO_PAINTED_RIVERS=1`, `MAP_IN_GAME=1`.

## Critical "don't break" rules surfaced this session

- `override_vectorized.png` MUST be a fresh copy of `override_final.png` before running upscale (workaround for stale composite).  See `upscale_override_vectorized.py:272`.
- `eco_density_mod` for `sand_dune_px` is 0.20 (was 0.05).  Don't revert to 0.05 — vegetation will disappear again.
- `_NON_PLANTABLE` set in `surface_decorator.py:2139` MUST NOT include sand/red_sand/suspicious_sand.  They're plantable in MC 1.21 (cactus, dead_bush, dry grass).  This was the bug that killed ALL dune vegetation.
- `TRUNK_RUN_FRAC` = 0.85 in `chunk_writer.py:1053`.  Lower (0.6) didn't filter enough branch-trunks.  Higher might break legit multi-trunk schemas — needs walk validation.
- `ALIGN_SCALE = 1.00` in `upscale_override_vectorized.py:61`.  Was 1.01, caused 100-500 block painted-feature drift.
- `MEDIAN_KERNEL = 9` (was 17) — less aggressive median in upscale.

## Quick-reference commands (for next session)

### Fire final tile chain (22 tiles + 4 ocean-replacement)
```bash
# Use memory/biome_reference_tiles.csv to build the tile list.
# Skip tiles already rendered with all fixes (DRY_OAK 29,76, LUSH 6,68, KARST 34,9, SAND_DUNE 18,66).
# Render: NEW reference tiles for EASTERN_TEMPERATE_COAST(28,35), TIDAL_JUNGLE_FRINGE(31,89), SCRUBBY_HEATHLAND(85,79), SEMI_ARID_SHRUBLAND(27,65)
# + re-render any biomes the user wants double-checked with all latest fixes.
```

### Re-render world map after override changes
```bash
PYTHONUNBUFFERED=1 "C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe" tools/diag_world_map_comprehensive.py
```

### Re-run upscale (workaround needed if user's saved new override paint)
```bash
cp /c/Users/nicho/minecraft-worldgen/override_final.png /c/Users/nicho/minecraft-worldgen/override_vectorized.png
PYTHONUNBUFFERED=1 "C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe" /c/Users/nicho/minecraft-worldgen/upscale_override_vectorized.py
```

### Re-build lithology (after override changes or paint)
```bash
PYTHONUNBUFFERED=1 "C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe" tools/build_lithology.py --masks "C:/Users/nicho/minecraft-worldgen/masks" --config config/thresholds.json
```

## Latest map URLs (24h expiry from approx 16:00 Apr 26)
- Biome-only map: https://litter.catbox.moe/75ky35.jpg
- Full map (in-game widths): https://litter.catbox.moe/qdvkar.jpg
- Walk-map (red tile boxes): https://litter.catbox.moe/367mlp.jpg

## Session walk-feedback issues + resolutions

| Issue | Status |
|---|---|
| Map showed stale biomes (4 days old) | Fixed via upscale-input swap.  Override.tif now Apr 26 17:32 |
| Sand-tile coastline slivers (orange where should be lush) | Fixed via Phase 4.6 coastal repaint, 32-block band |
| Painted region edges digital-camo blocky | Fixed via `core/region_overlay_smoothing.py` median+jitter helper |
| Painted river additions look like clovers | Fixed via `clean_painted_river_mask` (opening + endpoint prune) |
| World map missing 97% of rivers | Fixed by reading hydro_centerline values 128 (wadi) + 255 (braid) |
| World map biome boundaries digital-camo | Fixed by `Resampling.mode` (majority vote) on override read |
| Lithology paintover gross-upscale | Fixed via smooth_region_paint applied at 8192 source |
| Oasis painted ring off-center from lake | Fixed by ALIGN_SCALE 1.01 → 1.00 |
| Painted biomes show wrong color where lakes/floodplain wins | Working as intended (in-game lakes are water, biome only affects land color) |
| Trees over rivers floating without trunk | Code-fix attempted (canopy anchor) but reverted; trees over rivers REJECTED whole stamp by S65 reject |
| Small trees in dry biomes "columns of leaves" | Plan B trunk threshold (0.85) helps but not perfect.  Schematic-level fix recommended |
| Sand dune literal zero vegetation | Fixed by `_NON_PLANTABLE` removing sand/red_sand + eco_density_mod 0.20 |
| BOREAL_ALPINE altitude snow | S62 baseline retained; not regressed |
| Floodplain wipes out riparian/fen/lush trees | Fixed via biome-skip exception (Items C, E) |
| Sand dunes appearing at MANGROVE_COAST | Fixed via Item D strict gate |

## Final sanity-check facts to remember next session
- Worktree: `C:\Users\nicho\minecraft-worldgen\.claude\worktrees\pensive-mirzakhani-3da700`
- Python: `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe`
- Masks: `C:\Users\nicho\minecraft-worldgen\masks\` (NOT in worktree)
- Test world: `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\`
- Plan file (this session): `C:\Users\nicho\.claude\plans\starry-cuddling-moth.md`

## Mood notes
User got frustrated multiple times with iteration loops on tree + sand_dune issues.  When unsure, take a beat, propose multiple options, get user signoff before implementing.  User likes the Workshop pattern: "triage and suggest fixes for both before implementing anything".

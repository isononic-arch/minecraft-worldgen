# PROJECT BIBLE — Vandir World Generation Pipeline
*Last updated: 2026-03-18 (Session 12)*
*For use by parallel Claude Code instances and future sessions.*

---

## 1. OVERVIEW

**World name:** Vandir
**Minecraft version:** 1.20.1 + Higher Heights datapack (Y range -64 to 448)
**World size:** 50,000 × 50,000 MC blocks (1:1 pixel-to-block)
**Tile size:** 512 × 512 px — 97×97 grid = 9,409 tiles total
**Source DEM:** Gaea heightmap, 8192×8192 → upscaled to 50k×50k

---

## 2. CRITICAL PATHS

| Item | Path |
|------|------|
| Project root | `C:\Users\nicho\minecraft-worldgen\` |
| Python (ONLY one with all deps) | `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe` |
| All 50k×50k masks | `C:\Users\nicho\minecraft-worldgen\masks\` |
| Config | `config\thresholds.json` |
| Schematic index | `schematic_index.json` |
| Pipeline core modules | `core\` |
| Tools / diagnostics | `tools\` |
| Output region files | `output\` |

**⚠️ The project root `C:\Users\nicho\masks\` does NOT exist. All masks are under the project root.**

---

## 3. MASK FILES (all 50k×50k GeoTIFF, `masks/`)

| File | Description | dtype | Notes |
|------|-------------|-------|-------|
| `height.tif` | Gaea DEM, 16-bit | uint16 | LOW value = HIGH terrain |
| `slope.tif` | Derived slope | float32 | 0.0–1.0 |
| `river.tif` | River/stream mask | uint8 | |
| `rivers.tif` | River flow field | float32 | |
| `flow.tif` | Moisture/flow proxy | float32 | 0.0–1.0 |
| `erosion.tif` | Erosion mask | float32 | 0.0–1.0 |
| `shore.tif` | Shoreline proximity | uint8 | |
| `deposits.tif` | Alluvial deposits | float32 | |
| `override.tif` | Zone/biome override | uint8 | **Active — rebuilt from override_vectorized.png** |

All masks read via `rasterio.Window()` — **never full-load**. No exceptions.

---

## 4. HEIGHT MASK POLARITY (LOCKED — DO NOT CHANGE)

```
LOW raw 16-bit  = HIGH terrain (mountains, peaks)
HIGH raw 16-bit = LOW terrain (ocean floor)

Sea level  = raw 17050  → MC Y 63
Raw 0      → MC Y 448  (peak)
Raw 65535  → MC Y -10  (ocean floor)

Terrain spline (4-point, locked):
  gaea_in  = [65535, 17050, 8000,  0  ]
  mc_y_out = [-10,   63,    200,   448]
```

This is baked into `config/thresholds.json → terrain_spline` and `core/column_generator.py → _build_lut_vectorized()`.

---

## 5. OVERRIDE SYSTEM

### Source files
| File | Size | Description |
|------|------|-------------|
| `override_final.png` | 8192×8192 L | Original hand-painted, discrete zone values |
| `override_vectorized.png` | 8192×8192 RGBA | Bezier-smooth boundaries (R==G==B==zone_value) |
| `override_final_pre_vectorized.png` | 8192×8192 L | Backup before vectorized work |
| `masks/override.tif` | 50k×50k uint8 | **The active override mask consumed by the pipeline** |

### How override.tif is built
Script: `upscale_override_vectorized.py`

1. Load `override_vectorized.png` → R channel (zone values, 0–240)
2. Load `override_final.png` → R channel (zone fill for interiors)
3. Composite: use vectorized where non-zero, fall back to base fill elsewhere
4. **`np.fliplr()`** — flip X axis to match height.tif coordinate system (CRITICAL)
5. Bilinear upscale 8192→50000 in 256-row chunks
6. LUT-quantize every pixel to nearest valid zone code (prevents phantom zones)
7. Write as BigTIFF with zlib compression

### Zone values (27 zones)
```
0=none (no override → height/slope logic)
10=COASTAL_HEATH       20=TEMPERATE_RAINFOREST  30=BOREAL_TAIGA
35=SNOWY_BOREAL_TAIGA  40=ALPINE_MEADOW         50=ARCTIC_TUNDRA
55=FROZEN_FLATS        60=TEMPERATE_DECIDUOUS   70=RAINFOREST_COAST
80=RIPARIAN_WOODLAND   90=DRY_OAK_SAVANNA       100=KARST_BARRENS
110=BIRCH_FOREST       115=EASTERN_TEMPERATE_COAST  120=MIXED_FOREST
130=CONTINENTAL_STEPPE 140=DRY_PINE_BARRENS     150=SCRUBBY_HEATHLAND
160=LUSH_RAINFOREST_COAST  170=SAND_DUNE_DESERT  190=DESERT_STEPPE_TRANSITION
200=SEMI_ARID_SHRUBLAND    210=DRY_WOODLAND_MAQUIS  220=TIDAL_JUNGLE_FRINGE
230=MANGROVE_COAST         240=FRESHWATER_FEN
```

### CRITICAL RULES for override
1. **NEVER apply Gaussian blur to zone values before LUT quantization.** Zone codes are discrete labels (0,10,20…240), not a continuous spectrum. Blurring creates intermediate values (e.g. 65 at a 10/120 boundary) that snap to phantom biomes (FROZEN_FLATS=55 appears as near-white bands). Blur is applied to RGBA display output only.
2. **NEVER use nearest-neighbour upscale for override_vectorized.png.** Bilinear is required for smooth bezier boundary curves.
3. **ALWAYS flip X axis** (`np.fliplr`) when building override.tif from the PNGs — the PNGs are east/west mirrored relative to height.tif.
4. **NEVER touch override_final.png** — it is the master source. All pipeline output goes to override.tif.

---

## 6. PIPELINE ARCHITECTURE

### Step execution order

```
Step 0  — Height histogram diagnostic (step0_diagnostic.py) ✅
Step 1  — Height mask validation ✅
Step 2  — Slope mask derivation ✅
Step 3  — River/flow mask ✅
Step 4  — Erosion/deposits mask ✅
Step 5  — Biome assignment (core/biome_assignment.py) ✅
Step 6  — Column generator (core/column_generator.py) ✅
Step 6a — River carver (core/river_carver.py) ✅
Step 7  — Surface decorator (core/surface_decorator.py) ✅
Step 8  — Schematic placement (core/schematic_placement.py) ✅
Step 9  — Chunk writer (core/chunk_writer.py) ✅ (nbtlib rewrite)
Step 10 — CLI entry point / tile parallelism (run_pipeline.py) ✅
Steps 1–13: ALL COMPLETE
Step 14 — Full 50k×50k production run: BLOCKED pending override.tif alignment validation
```

### Entry point
```bash
python run_pipeline.py --config config/thresholds.json \
    --masks masks --schem-index schematic_index.json \
    --output output/ [--threads N] \
    [--tile-x0 TX --tile-x1 TX --tile-z0 TZ --tile-z1 TZ] \
    [--dry-run]
```

### IPC protocol (stdout only — do not add prints)
```json
{"type": "tile_start",    "tile_x": 4, "tile_y": 7}
{"type": "tile_complete", "tile_x": 4, "tile_y": 7, "biomes": [...], "elapsed_ms": 4821}
{"type": "tile_error",    "tile_x": 4, "tile_y": 7, "error": "..."}
{"type": "pipeline_complete", "total_tiles": 9604, "elapsed_s": 7320}
```

### Architecture rules (non-negotiable)
- No PyQt6 / GUI imports anywhere in `core/` or `run_pipeline.py`
- No full raster loads — all mask reads via `rasterio.Window()`
- `sys.stdout.flush()` after every JSON line
- Tile workers are independent processes (`ProcessPoolExecutor`)
- Always call `process_tile_columns_v2()` — **never** old `process_tile_columns()` (21 min vs 80s)

---

## 7. CORE MODULE DETAILS

### `core/biome_assignment.py` — Step 5
4-stage pipeline per tile:
- Stage 0: Override check (short-circuits if override_tile != 0)
- Stage 1: Height + Slope → Terrain class
- Stage 2: Flow + Erosion → Hydrology override
- Stage 3: Flow (moisture proxy) + Altitude → Forest/Biome resolution

All thresholds from `config/thresholds.json`. Sea level 16-bit = 17050.

### `core/column_generator.py` — Step 6
Generates full vertical block column (Y -64 to 448) per pixel.

Column layout (locked):
```
Y = -64                  → bedrock
Y = -63 .. surface_Y-3   → stone (cliff banding on steep columns)
Y = surface_Y-2, -1      → subsurface block (biome-dependent)
Y = surface_Y            → surface block (biome + mask + noise)
Y = surface_Y+1 .. 63   → water (MANDATORY if surface_Y < SEA_LEVEL)
Y > 63                   → air
```

Features: sand dune geometry for SAND_DUNE_DESERT, snow cap variation, cliff face banding, underwater floor graduation.

### `core/surface_decorator.py` — Step 7
Per-biome block palette (surface + subsurface block mixing):
- Priority: altitude > erosion2 > erosion > moisture2 > moisture > noise3 > noise2 > noise > base
- River bank features: mud/clay strip, gravel bars on bends
- Ground cover placement via decoration density noise

### `core/chunk_writer.py` — Step 9 (nbtlib rewrite — no amulet)
Key constants:
```python
Y_MIN = -64, Y_MAX = 448, SEA_Y = 63
Y_RANGE = 512   # CRITICAL: vol shape must be (512, tile_h, tile_w)
N_SECTIONS = 32
DataVersion = 3465  # MC 1.20.1
```
- Uses nbtlib (no amulet dependency)
- 1.18+ padded long-array format for BlockStates
- zlib compression type 2, `_SECTOR_SZ = 4096`
- `except Exception: continue` in `write_tile_to_region` suppresses per-chunk errors (by design)
- **A vol shape of (384, ...) will silently produce empty .mca files** — always use 512

### `core/schematic_placement.py` — Step 8
Anchor system: `place_y = terrain_surface_y - anchor_y - inset_depth`
Y-variation weights by size (sm/md/lg). See `PLACEMENT_VARIATION_SPEC.md`.

**Known bug:** `load_index()` type mismatch — `'str' object has no attribute 'get'` (minor, low priority)

---

## 8. CONFIG: `config/thresholds.json`

Key sections:
- `terrain_spline` — 4-point locked spline matching height polarity
- `terrain_class` — height/slope thresholds for biome stage 1
- `slope_thresholds` — `steep: 0.65`, `very_steep: 0.35` (FIXED Session 8 from 0.95/0.85)
- `sea_level_16bit` — 17050
- `decoration_density`, `sparse_overrides`, `surface_palettes` — decoration controls

---

## 9. TOOLS

### `tools/terrain_preview.py` — Interactive tile viewer (Steps 1–10 DONE)
```bash
python tools/terrain_preview.py
```
- Render modes: `surface_height` | `biome` | `slope` | `surface_block`
- Mouse: scroll=zoom, drag=pan, double-click=reset
- Reads from `masks/` via rasterio Window
- Biome/surface_block render reads from `masks/override.tif` directly (no fliplr needed — baked into TIF)
- RGBA Gaussian blur sigma=1.2 applied to biome/surface_block output for visual softening only (does NOT affect data)

### `tools/override_aligner.py` — Override alignment GUI
```bash
python tools/override_aligner.py
```
- Loads height.tif (subsampled 800×800) and override_final.png (8192×8192)
- Controls: X offset, Z offset, Scale, Sigma (boundary blur for preview only)
- Flip X checkbox (default: checked), Flip Z checkbox
- Scroll zoom up to 20×, left-drag pan, double-click reset
- Save button: runs full 50k upscale in background thread → writes `masks/override.tif`
- **Does NOT touch override_final.png**

### `tools/validate_test_tile.py` — Single-tile validator
```bash
python tools/validate_test_tile.py \
    --config config/thresholds.json \
    --masks masks \
    --schem-index schematic_index.json \
    --output output \
    --tile-x 48 --tile-z 48 \
    --report validation_report \
    --dry-run
```
Last good result (tile 48,48): **8 PASS, 0 FAIL, 2 WARN, ~80s**

---

## 10. COORDINATE SYSTEM

```
World pixel (0,0)   = NW corner of world
World pixel (49999,49999) = SE corner
Tile (0,0)          = NW-most tile
Tile (48,48)        = center test tile

Tile pixel offset:
  world_px = tile_x * 512 + local_x
  world_pz = tile_z * 512 + local_z

Override/height alignment:
  height.tif: (0,0) = NW, X increases east, Z increases south
  override_vectorized.png: X axis WAS mirrored — FIXED via np.fliplr in upscale script
  override.tif: CORRECT orientation matching height.tif
```

---

## 11. ALL FIXED BUGS (chronological)

| Session | Bug | Fix |
|---------|-----|-----|
| 5 | Height polarity inverted | Confirmed LOW=HIGH terrain. Locked spline. |
| 5 | Spline mismatch | `_build_lut_vectorized()` now uses 4-point locked spline [0,8000,17050,65535]→[448,200,63,-10] |
| 7 | Sea level off | Locked gaea=17050→MC Y63 |
| 7 | Unicode crash in validate_test_tile.py | `io.TextIOWrapper(encoding='utf-8')` on stdout |
| 8 | amulet not installed | Rewrote `core/chunk_writer.py` using nbtlib |
| 8 | Slope thresholds wrong (0.95/0.85) | Fixed to 0.65/0.35 in thresholds.json |
| 8 | Tuff preview colour wrong | Olive-green (0x82,0x8C,0x58) — distinct from stone |
| 11 | Phantom zones at biome boundaries (white bands) | Removed Gaussian pre-blur from upscale_override_vectorized.py entirely |
| 11 | Override X-axis mirrored vs height.tif | Added `np.fliplr(composite)` in upscale_override_vectorized.py |
| 11 | Tried Gaussian blur on override.tif zone values (sigma=14) | **Wrong fix — deleted override_smoothed.tif.** Zone values are discrete labels, not a spectrum. |

---

## 12. KNOWN / OPEN ISSUES

| Priority | Issue | Notes |
|----------|-------|-------|
| HIGH | Override spatial offset (~10%) | Use override_aligner.py to find correct X/Z offset + scale, then Save |
| MEDIUM | Schematic placement wiring | `load_index()` type mismatch — 'str' object has no attribute 'get' |
| LOW | Wire sparse_overrides + surface_palettes to pipeline | surface_decorator.py needs ~20 lines to read from thresholds.json |

---

## 13. NEXT STEPS (in order)

1. **Use `tools/override_aligner.py`** to visually align override to terrain → Save → regenerates `masks/override.tif`
2. **Re-run validator** on tile (48,48) — confirm 8 PASS, 0 FAIL
3. **Fix schematic wiring** — load_index() type mismatch
4. **Wire surface_palettes/sparse_overrides** to surface_decorator.py
5. **Step 14** — Full 50k×50k production run (ProcessPoolExecutor, all 9,409 tiles)

---

## 14. DEPENDENCY NOTES

- Python: `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe` — ONLY this install has all deps
- Key packages: `rasterio`, `nbtlib`, `numpy`, `Pillow`, `scipy`, `opensimplex`, `PyQt6`
- `amulet` is NOT installed and NOT required (chunk_writer.py was rewritten to use nbtlib)
- `rasterio` will emit `NotGeoreferencedWarning` — safe to ignore (world has no geotransform)

---

## 15. FILE MAP (key files)

```
minecraft-worldgen/
├── run_pipeline.py              # Step 10 CLI entry, ProcessPoolExecutor tile dispatch
├── upscale_override_vectorized.py  # Builds masks/override.tif from override_vectorized.png
├── config/
│   └── thresholds.json          # All pipeline thresholds — source of truth
├── core/
│   ├── biome_assignment.py      # Step 5: 4-stage biome logic
│   ├── column_generator.py      # Step 6: vertical block column fill
│   ├── river_carver.py          # Step 6a: river/lake carving
│   ├── surface_decorator.py     # Step 7: surface block palette mixing
│   ├── schematic_placement.py   # Step 8: tree/structure stamping
│   ├── schematic_loader.py      # NBT schematic reader
│   ├── chunk_writer.py          # Step 9: nbtlib chunk → .mca writer
│   ├── tile_streamer.py         # Streams mask tiles via rasterio Window
│   ├── noise_fields.py          # OpenSimplex noise generators
│   └── preview_renderer.py      # Off-screen render helpers (no GUI)
├── tools/
│   ├── terrain_preview.py       # PyQt6 interactive viewer (all 4 render modes)
│   ├── override_aligner.py      # PyQt6 alignment GUI for override.tif
│   ├── validate_test_tile.py    # Single-tile validator
│   └── diag_coord_alignment.py  # Coordinate system diagnostic
├── masks/
│   ├── height.tif               # 50k×50k, uint16
│   ├── slope.tif, river.tif, flow.tif, erosion.tif, shore.tif, deposits.tif
│   └── override.tif             # 50k×50k, uint8 — THE active override mask
├── override_vectorized.png      # 8192×8192 RGBA, bezier-smooth zone boundaries
├── override_final.png           # 8192×8192 L, original hand-painted (DO NOT MODIFY)
├── schematic_index.json         # All schematics with anchor/inset metadata
├── PLACEMENT_VARIATION_SPEC.md  # Y-variation rules for schematic placement
└── validation_report/           # PNG screenshots from validator runs
```

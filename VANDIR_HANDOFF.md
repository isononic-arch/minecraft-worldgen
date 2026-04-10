# Vandir World Generation — Project Handoff / Carrier Doc
*Generated 2026-03-27 (Session 20). For use in Gemini Code or any fresh agent context.*

---

## What This Project Is

**Vandir** is a 50,000 × 50,000 block Minecraft world built as a **terrain translation pipeline**, not a procedural generator. Source heightmaps and masks come from Gaea (a terrain DCC tool) and are translated 1:1 into Minecraft chunk data. The world has never been inhabited — no roads, no structures, no human marks. 1 block = 1 metre. Tradewinds blow west to east (global constant).

**Scale:** 50k×50k blocks, 512×512 per tile, 97×97 = 9,409 tiles total.
**Vertical range:** Y -64 to Y 448 (custom `vandir_height.zip` datapack, NOT HigherHeightsUltimate).
**Sea level:** raw 17050 / MC Y 63.
**DataVersion:** 4556 (MC 1.21.10).

---

## Python Environment

**Always use:** `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe`
No other Python install has: rasterio, nbtlib, PyQt6, scipy, opensimplex, Pillow.

---

## Directory Layout

```
C:\Users\nicho\minecraft-worldgen\
├── masks\                        # 50k×50k TIFs (NEVER full-load — always rasterio.Window)
│   ├── height.tif                # 16-bit, HIGH value = HIGH terrain
│   ├── flow.tif                  # moisture proxy [0,1]
│   ├── erosion.tif               # [0,1]
│   ├── slope.tif                 # [0,1]
│   └── override.tif              # uint8 zone codes (rebuilt with upscale_override_vectorized.py)
├── core\
│   ├── biome_assignment.py       # 4-stage biome logic
│   ├── column_generator.py       # vertical block column builder
│   ├── chunk_writer.py           # nbtlib MCA writer
│   ├── noise_fields.py           # OpenSimplex generators
│   └── surface_decorator.py      # surface block palettes per biome
├── tools\
│   ├── world_studio.py           # MAIN TOOL — integrated single-pane worldgen studio
│   ├── validate_test_tile.py     # single-tile validator (USE THIS, not run_pipeline.py)
│   ├── terrain_preview.py        # legacy 4-mode viewer
│   ├── override_aligner2.py      # drag-to-align override.tif tool
│   └── check_tile_seams.py       # boundary seam checker
├── config\
│   └── thresholds.json           # ALL thresholds — source of truth, never hardcode
├── run_pipeline.py               # BROKEN (float bug) — do not use for tile gen
├── upscale_override_vectorized.py # rebuilds masks/override.tif
├── override_vectorized.png        # source override art (vectorized zone borders)
├── override_final.png             # PROTECTED master fill source — never modify
└── CLAUDE.md                      # authoritative project rules (always read this)
```

---

## Height Polarity (CRITICAL — was inverted until Session 20)

```
HIGH raw 16-bit  =  HIGH terrain (mountains)
LOW  raw 16-bit  =  ocean floor
Sea level        =  raw 17050  →  MC Y 63
Spline:  gaea_in=[0, 17050, 45000, 65496]  →  mc_y_out=[-64, 63, 200, 448]
Normalised:  height_norm = raw / 65535   →   0.0 = deepest ocean, 1.0 = highest peak
Sea norm ≈ 0.260   (NOT 0.740 — the old inverted value was wrong)
```

Any code that says `t_height = 1.0 - height_norm` or `sea_norm ≈ 0.740` is using the **old wrong polarity** and must be fixed.

---

## Pipeline Stages

| Stage | File | Notes |
|---|---|---|
| 0 | Gaea export | heightmap + flow + erosion + slope TIFs |
| 1 | `upscale_override_vectorized.py` | builds `masks/override.tif` from 8192 source PNGs |
| 2 | `core/biome_assignment.py` | assigns biome string per pixel (4-stage pipeline) |
| 3 | `core/column_generator.py` | builds vertical block columns from height spline + biome |
| 4 | `core/surface_decorator.py` | surface block palettes, cliff banding, alpine exposure |
| 5 | `core/chunk_writer.py` | writes .mca chunk files via nbtlib |
| Validate | `tools/validate_test_tile.py` | run this for single-tile test, NOT run_pipeline.py |

---

## Biome Assignment (`core/biome_assignment.py`)

4-stage pipeline per tile:

**Stage 0 — Override:** `override.tif` uint8 zone code → biome string (short-circuits if non-zero).
**Stage 1 — Terrain class:** height_norm + slope → `ocean / coastal / lowland / highland / alpine / ice_cap`.
**Stage 2 — Hydrology:** high flow + low slope → `RIPARIAN_WOODLAND`; mid flow coastal → `FRESHWATER_FEN`.
**Stage 3 — Biome resolve:** terrain class × flow (moisture proxy) → final biome string.
**Layer A — Patch noise:** fBm noise downgrades some forest/steppe pixels for variation.

**Terrain class thresholds** (fraction of land range above sea_norm≈0.260):
- coastal: up to +35% of land range → norm ≤ 0.519
- lowland: up to +55% → norm ≤ 0.667
- highland: up to +72% → norm ≤ 0.793
- alpine: up to +88% → norm ≤ 0.906
- ice_cap: above that

**Override zone codes** (partial list): 0=none, 10=COASTAL_HEATH, 20=TEMPERATE_RAINFOREST, 30=BOREAL_TAIGA, 40=ALPINE_MEADOW, 50=ARCTIC_TUNDRA, 60=TEMPERATE_DECIDUOUS, 80=RIPARIAN_WOODLAND, 100=KARST_BARRENS, 120=MIXED_FOREST, 130=CONTINENTAL_STEPPE, 170=SAND_DUNE_DESERT, 230=MANGROVE_COAST, 240=FRESHWATER_FEN.

**Phantom biome cleanup** in `assign_biomes()`:
- Connected-component labeling per zone code
- Components < 400px flooded with dominant surrounding zone (8px dilation halo)
- Eliminates jitter-scatter blobs (single source-pixel × 6× upscale = 36px) definitively

---

## Override Rules (CRITICAL)

1. **NEVER blur zone values** before LUT quantization — blurring discrete codes creates phantom biomes.
2. **ALWAYS use `Image.NEAREST`** for zone code upscaling — bilinear creates intermediate values (e.g. 50 between zone 0 and 100) that LUT-snap to phantom zone codes (ARCTIC_TUNDRA, etc.).
3. **ALWAYS `np.fliplr()`** override source — source PNGs are X-mirrored vs height.tif.
4. **NEVER modify `override_final.png`** — protected master. Write only to `masks/override.tif`.
5. Rebuild `masks/override.tif` with: `python upscale_override_vectorized.py`

---

## Chunk Writer Rules (CRITICAL — hard-won)

1. **Biome PalettedContainer `min_bits=1`**, NOT 4. Using 4 → "Invalid length given for storage" → world fails to load on every chunk.
2. **No SkyLight/BlockLight** — `isLightOn=0`, let MC recompute.
3. **Fluid ticks: top water block only** — all-column ticks cause 1M+ ticks on ocean tiles → MC hangs on load.
4. **Test world spawn in void** (12000, 100, 12000) — approach tiles from outside to allow gradual loading.

---

## Known Bugs

### CRITICAL — `run_pipeline.py` float bug (DO NOT USE)
`run_pipeline.py` passes `float32 [0,1]` height to `generate_columns` which casts to `uint16` → all zeros → all terrain at Y=-60 (underwater). **Use `validate_test_tile.py` instead** — it un-normalises correctly before calling `process_tile_columns_v2`.

### Phase 3 — Seam Issues (deferred)
- Z-seam tile (49,46): max=11 blocks — ocean depth EDT computed tile-locally, sees different shore distance at tile edge than neighbour
- Z-seam tile (57,46): max=6 blocks — minor
- Fix: global EDT pass or overlapping-window EDT

---

## World Studio (`tools/world_studio.py`)

The main integrated tool. Launch: `python tools/world_studio.py`

**What's working:**
- **Tool A — World Map**: zoomable 97×97 grid, height LOD thumbnails, tile selection, Height/Biome layer toggle, river overlay (Extract Rivers + Draw River)
- **Tool C — Tile Inspector**: coords, world block range, MCA filename in status bar
- **Tool E — Preview**: numpy hillshade (FastIsoLoader, instant on tile select); SimPreviewWorker fires biome-only sim ~3-5s auto on tile select; "Generate MCA" runs full column export. Sun az/exag sliders.
- **TileCanvas**: biome hover pill (mouseover shows biome name); QWidget+paintEvent (not QLabel — avoids layout cascade)
- **Cluster view**: 3×3 / 5×5 / 9×9 toggle; BiomeClusterLoader gives biome-colored cluster; "Biome" toggle blends 65% biome + 35% hillshade
- **Config panel**: scrollable (QScrollArea wraps ConfigPanel — fixes clip bug); thresholds.json sliders
- **River layer**: RiverExtractWorker reads flow.tif, thresholds at 85th percentile, labels components, traces 8-connected paths, assigns Strahler-like order; blue=extracted, cyan=manual draw

**Known issues in world_studio:**
- Cross-section sometimes fails (PIL late import + state race — see tooling gaps doc for fix plan)
- Generate MCA ("Generate Colors") broken until `run_pipeline.py` float bug is fixed
- No render-status overlay yet (grey/amber/green per tile — planned next)
- No config histogram backing yet (planned next after status overlay)

---

## Next Steps (Priority Order)

### Immediate (Phase 2 — World Studio)
1. **Render-status overlay** — `RenderManifest` class reads/writes `output/render_manifest.json` mapping `"tx,tz"` → `{status, timestamp, config_hash}`. `WorldMapView` paints grey/amber/green semi-transparent overlays per tile. Every `GenerateWorker.finished` writes green; every config save writes amber for all tiles.
2. **Config histogram backing** (Tool B) — histogram behind each threshold slider, sampled at startup from mask TIFs (every 16th pixel across 50k grid, ~2s, cached). Threshold line moves live as slider moves.
3. **Fix `run_pipeline.py` float bug** — pass uint16 or detect float in `generate_columns` and un-normalise before uint16 cast.

### Small Phase 1 Remainder
4. **CONTINENTAL_STEPPE stone** — change from andesite to granite in `core/surface_decorator.py` BIOME_BLOCK_PALETTES.
5. **DRY_OAK_SAVANNA stone** — add terracotta/red_sand for laterite feel.

### Phase 3 (Global Planning — deferred)
- River network extraction (Strahler ordering → river_plan.geojson)
- Lake placement (tarns, valley, lowland, ox-bow)
- River delta plan (order 3+ rivers at coast)
- Fix ocean depth EDT seam at tile (49,46) — global EDT or overlapping window
- Aspect-based snow line + wind_exposure.tif (tradewind shadow)

### Phase 4 (Render)
- Full 50k MCA export (ProcessPoolExecutor, content-hash checkpointed)
- Post-render validation (seam, river continuity, lake integrity, height cap)

### Phase 5 (Quality)
- Structure placer (Tool F) — schematic overlay on world map, drag/snap, auto-place
- Annotation layer (Tool G) — click-to-note, severity filter, resolve tracking
- Biome Studio (Tool D) — height/slope scatterplot, draggable thresholds, real-time recolour
- Biome override painting on world map (port from override_aligner2.py drag logic)

---

## Test Tile Coordinates

| Tile | TP Command | Character |
|---|---|---|
| (48,48) | `/tp @s 24832 200 24832` | All ocean |
| (50,46) | `/tp @s 25856 200 23808` | Coastal / mixed |
| (56,46) | `/tp @s 28928 200 23808` | All land |

Test world: `Vandirtest5`
Save path: `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest5\`

---

## Key Constants

```
Sea level:       raw 17050  →  MC Y 63
Height spline:   gaea=[0,17050,45000,65496]  →  mc_y=[-64,63,200,448]
World size:      50,000 × 50,000 blocks
Tile size:       512 × 512 blocks
Grid:            97 × 97 = 9,409 tiles
MC Y range:      -64 to 448
DataVersion:     4556  (MC 1.21.10)
Slope thresholds: steep=0.65, very_steep=0.35  (in thresholds.json)
Override upscale: NEAREST (never bilinear — creates phantom biomes)
Override jitter:  3 passes, prob=0.5, seed=42, applied at 8192 source resolution
```

---

## Architectural Vision Summary

The full vision lives in `ARCHITECTURE_VISION.md`. Key principle: **the voxel preview IS the feedback loop** — the 50k MCA export is one-click final output, not an iteration surface. The studio should feel like a world you explore, not a generator you run.

Integrated tool suite (all in `world_studio.py`):
- **Tool A** — World Map (zoomable, all layers, river/annotation overlay) ✅ built
- **Tool B** — Live Config Panel (histograms behind every threshold) 🔲 next
- **Tool C** — Tile Inspector (coords, block range, MCA name) ✅ built
- **Tool D** — Biome Studio (scatterplot, draggable thresholds) 🔲 Phase 5
- **Tool E** — 3D Preview (hillshade + biome sim, instant on tile select) ✅ built
- **Tool F** — Structure Placer 🔲 Phase 5
- **Tool G** — Annotation Layer 🔲 Phase 5

The **single highest-leverage remaining change**: render-status overlay (grey/amber/green per tile on world map). Makes the tool feel live — you know at a glance which tiles are fresh, which are stale, which haven't been touched.

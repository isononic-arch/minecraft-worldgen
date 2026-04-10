# Terrain Preview Tool — Build Spec

## Purpose
A standalone PyQt6 diagnostic and tuning tool for the Vandir pipeline. Allows real-time terrain preview and parameter editing without writing `.mca` files. Replaces the current workflow of editing `thresholds.json` → re-running `validate_test_tile.py` → inspecting static PNGs.

---

## Environment
- **Python:** `C:\Users\nicho\AppData\Local\Python\bin\python.exe`
- **Project root:** `C:\Users\nicho\minecraft-worldgen\`
- **Masks:** `C:\Users\nicho\masks\` (use Windows path — never `/c/` prefix)
- **Config:** `config/thresholds.json` — all parameters read from here, never hardcoded
- **Existing framework:** `gui/app.py` (PyQt6, 7-panel) — do NOT import from or modify this file. Build as a fully standalone script at `tools/terrain_preview.py`
- **Core modules available:** `core/column_generator.py` (process_tile_columns_v2), `core/biome_assignment.py`, `core/tile_streamer.py`, `core/surface_decorator.py`

---

## Architecture

Single standalone PyQt6 window. Three panels arranged as:

```
┌─────────────────────┬──────────────────┐
│                     │                  │
│   MAP VIEW (left)   │  CROSS-SECTION   │
│   zoomable 2D       │  (top right)     │
│   3×3 tile grid     │                  │
│                     ├──────────────────┤
│                     │                  │
│                     │  CONTROL PANEL   │
│                     │  (bottom right)  │
└─────────────────────┴──────────────────┘
```

---

## Panel 1 — Map View

- Renders a 3×3 grid of tiles centred on the selected tile (tile_x, tile_z)
- Each tile is 512×512 pixels, downsampled to 256×256 for display (3×3 = 768×768 canvas)
- Render modes selectable via dropdown: `surface_block` | `surface_height` | `biome` | `slope`
- Pannable and zoomable via mouse drag and scroll wheel
- Clicking any pixel on the map updates the cross-section to profile that column's E-W slice
- Tile grid lines drawn as thin grey borders
- Tile coordinates shown as overlay text on each tile
- Uses `rasterio.open()` with `Window()` reads only — never loads full masks

---

## Panel 2 — Cross-Section View

- Displays a 2D elevation profile of a single row across the current tile
- X axis = west→east pixel position (0–511)
- Y axis = MC Y coordinate (-64 to 448)
- Drawn as a filled polygon (terrain silhouette)
- Cyan horizontal line at Y=63 (sea level)
- Updates when:
  - User clicks a point on the map view (profiles that row)
  - Any control panel parameter changes (re-runs column generator on current row)
- Shows surface_y values only — not full column, just the terrain surface line
- Rendered using matplotlib embedded in PyQt6 (FigureCanvasQTAgg)

---

## Panel 3 — Control Panel

Three tabs:

### Tab A — Spline Editor
- Visual curve editor: draggable breakpoints on a 2D canvas
  - X axis = raw Gaea value (0–65535)
  - Y axis = MC Y (-64 to 448)
  - Locked breakpoints shown as filled circles, draggable
  - Curve drawn as smooth PCHIP interpolation between points
- Breakpoint table below curve: shows current (gaea_raw, mc_y) pairs
- "Apply" button — writes new breakpoints to `thresholds.json` and triggers cross-section refresh
- "Reset" button — reloads from `thresholds.json`
- Sea level line (raw=17050, Y=63) always shown as fixed reference — cannot be dragged

### Tab B — Surface Block Editor
- Biome selector dropdown (all 26 biomes)
- Per-biome palette table showing current block entries with:
  - Block name (editable text)
  - Condition type (base / noise / slope / erosion / altitude)
  - Threshold value (editable slider)
  - Weight/priority (editable)
- "Apply to preview" button — re-renders map view and cross-section with new palette
- Does NOT write to thresholds.json automatically — requires explicit "Save" button

### Tab C — Ocean/Depth
- `min_ocean_depth` slider (0–30 blocks)
- `ocean_depth_transition_px` slider (0–200 pixels)
- Live cross-section update on slider release (not on every tick)
- Shows depth profile in cross-section as blue fill below sea level

---

## Performance Requirements

- Cross-section update must complete in <3 seconds on tile (32,2)
- Map view tile render must complete in <10 seconds per tile
- Use `process_tile_columns_v2` for all column generation — never the old version
- All mask reads via `rasterio Window` — no full loads
- Tile renders cached — only re-render tiles whose parameters have changed
- Cross-section runs on a single row slice only, not the full 512×512 tile

---

## Constraints — Do Not Violate

- No PyQt6 imports in `/core` or `/pipeline` — tool is standalone
- No full mask loads — ever
- All thresholds from `thresholds.json` — never hardcoded
- Mask path always `r'C:\Users\nicho\masks\'` — never `/c/` prefix
- Use `process_tile_columns_v2` exclusively
- Noise seeds locked: biome_patch=42001, slope_mix=42003 — never change
- LUT and spline locked unless explicitly changed via spline editor

---

## Launch

```bash
C:\Users\nicho\AppData\Local\Python\bin\python.exe tools/terrain_preview.py --tile-x 32 --tile-z 2
```

Default tile: (32, 2). All other parameters loaded from `thresholds.json` on startup.

---

## Build Order for Claude Code

Build and smoke-test each component before proceeding:
1. Window scaffold + three-panel layout
2. Map view — single tile render (surface_height mode only)
3. Map view — 3×3 tile grid
4. Cross-section — static profile of tile (32,2) row 256
5. Cross-section — click-to-update from map view
6. Control panel Tab C (ocean depth) — simplest tab, good integration test
7. Control panel Tab A (spline editor)
8. Control panel Tab B (surface block editor)
9. Render modes (biome, surface_block, slope)
10. Polish: zoom, pan, tile coordinate overlays

---

*Do not build until chunk holes are resolved.*

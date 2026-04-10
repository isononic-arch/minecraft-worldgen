# Mask Pipeline Reference — Vandir World Generation
*Lightweight crash-recovery doc. Pull this up when resuming after a context loss.*

## Global Precompute Masks (1:8 → 50k)

All global masks are computed at 1:8 resolution (6250x6250) and upscaled to 50k (50000x50000).

| Mask | Script | dtype | Upscale | Values | Purpose |
|------|--------|-------|---------|--------|---------|
| `hydro_centerline.tif` | `rebuild_centerline.py` | uint8 | nearest | 0/1-5/128/255 | Strahler NMS + wadi + braid |
| `hydro_floodplain.tif` | `rebuild_floodplain.py` | uint8 | **bilinear** | 0/1 (soft edges) | River corridor clearings |
| `wind_windthrow.tif` | `rebuild_windthrow.py` | uint8 | **bilinear** | 0/1 (soft edges) | TPI ridge windthrow gaps |
| `rock_exposure.tif` | `rebuild_rock_exposure.py` | uint8 | **bilinear** | 0-255 gradient | Treeline → alpine → bare rock |
| `hydro_lake_wl.tif` | `generate_lake_wl.py` | float32 | nearest | spill elevation | Per-lake water level |

**All binary masks use bilinear upscale** for smooth edges (Session 39 decision). The `> 0.001` threshold in eco_gradients creates organic falloff at boundaries.

## Gap Mask System

### Priority Order (CRITICAL — order matters)
```
eco_gradients.py execution order:
  1. Floodplain (gap==4)  — river-constrained, applied first
  2. Rock exposure (gap==5,6) — elevation-driven, BEFORE windthrow
  3. Windthrow (gap==2)    — TPI ridges, AFTER rock exposure
  4. Meadow (gap==1)       — wet basins, applied last
```
Each type only claims `gap_mask == 0` pixels. Earlier types take priority.

### Gap Mask Values
```
0 = none (available for claiming)
1 = meadow clearing (wet basins, per-biome frequency)
2 = windthrow (exposed ridges, directional)
4 = floodplain corridor (river-width-modulated)
5 = bare rock (above treeline, steep cliffs)
6 = alpine meadow (treeline transition, wildflowers)
```
Value 3 was removed (bare patches superseded by rock_exposure gradient).

### Surface Block Treatment by Gap Type
| Gap | Surface Blocks | Dither | Final Override |
|-----|---------------|--------|----------------|
| 1 (meadow) | grass_block (95%), coarse_dirt (5%) | Standard ecotone | YES — absolute last pass |
| 2 (windthrow) | grass_block (97%), coarse_dirt (3%) | Standard ecotone | No |
| 4 (floodplain) | grass_block (92%), coarse_dirt (8%) | Standard ecotone | YES — absolute last pass |
| 5 (bare rock) | Probabilistic: stone/andesite/gravel/cobble/coarse_dirt vs grass_block | **Extended** (ramp /0.65) | No |
| 6 (alpine meadow) | Probabilistic: stone mix vs grass_block | **Extended** (ramp /0.65) | **NO** — removed Session 39 |

### Probabilistic Dither Formula (Rock/Alpine)
```python
_stone_prob = clip((rock_exposure_gradient - 0.3) / 0.65, 0.0, 1.0)
_is_stone = rock_or_alp & (random < _stone_prob)
```
- At gradient 0.3: 0% stone (all grass)
- At gradient 0.6: ~46% stone
- At gradient 0.95: 100% stone
- Extended ramp (/0.65 vs standard /0.55) = deliberately softer, grass pushes further uphill

## Surface Decoration Execution Order

```
decorate_surface() in surface_decorator.py:
  1. Noise layers / legacy palettes  (biome base blocks)
  2. Eco condition overlays          (terrain-driven: ridge, basin, moisture)
  3. Slope zone overrides            (cliff faces → stone)  [skips gap 1,4,5,6]
  4. Gap surface block ratios        (meadow→grass, windthrow→grass, rock→dither)
  5. River bank features             (mud, clay, gravel)
  6. Ecotone biome boundary dither   [skips gap 5,6]
  7. Ground cover placement          (species per gap type)
  8. Floating vegetation cleanup     (near water)
  9. Final meadow/floodplain override (gap 1,4 → grass_block, dilate 2px) [NOT gap 6]
```

## Ecotone Dither and Override Exclusions
- **Ecotone dither** (step 6): skips gap==5 and gap==6. Rock/alpine blocks are gradient-driven, not biome-palette-driven.
- **Final meadow override** (step 9): includes gap==1 and gap==4 ONLY. Does NOT include gap==6.
- **Slope zones** (step 3): excluded from gap 1, 4, 5, 6.

## Rebuild Checklist (after code changes)

1. **Code-only changes** (eco_gradients, surface_decorator): just regenerate tile
   ```bash
   python run_pipeline.py --config config/thresholds.json --masks masks \
     --schem-index schematic_index.json --output output \
     --tile-x0 36 --tile-x1 37 --tile-z0 20 --tile-z1 21 --threads 1
   ```

2. **Mask threshold/parameter changes**: rebuild the specific mask, then regenerate tile
   ```bash
   python rebuild_rock_exposure.py    # ~2 min, needs ~800MB RAM
   python rebuild_windthrow.py        # ~3 min, needs ~1GB RAM
   python rebuild_floodplain.py       # ~8 min, needs ~1GB RAM
   ```

3. **Copy to test world**:
   ```bash
   cp output/r.36.20.mca \
     "$APPDATA/ModrinthApp/profiles/test/saves/Vandirtest10/region/"
   ```

## Diagnostic Scripts
- `diag_rock_staircase.py` — 6-panel top-down: biome, gradient, gap_mask, conflict, elevation, boundary alignment
- `diag_floodplain_topdown.py` — 3x3 tile floodplain corridor visualization
- `diag_river_3x3_topdown.py` — 7x3 tile river carver output

## RAM Budget (8GB system)
| Task | Peak RAM | Notes |
|------|----------|-------|
| Single tile gen (1 thread) | ~500MB | Safe with browser open |
| Single tile gen (4 threads) | ~1.5GB | Close MC first |
| Mask rebuild (6250x6250) | ~1GB | Close browser + MC |
| Two tiles parallel | ~2GB+ | Often OOMs on 8GB |

## Key Paths
- Python: `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe`
- Masks: `C:\Users\nicho\minecraft-worldgen\masks\`
- Config: `C:\Users\nicho\minecraft-worldgen\config\thresholds.json`
- Test world: `C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\`

## Test Tiles & TP Coords
| Tile | Purpose | TP Command |
|------|---------|------------|
| (36,20) | Rock exposure, treeline | `/tp @s 18432 200 10240` |
| (59,53) | Windthrow | `/tp @s 30208 200 27136` |
| (51,53) | Floodplain, lakes | `/tp @s 26112 200 27136` |

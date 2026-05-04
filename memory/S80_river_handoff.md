# S80 River Pipeline — Painted-Source Handoff
*Session 80 (2026-05-03). Multi-day arc, exhaustive iteration on river generation.*

## TL;DR

**`masks/hydro_region.png` is now the SOLE source of rivers.** The user paints rivers in `tools/override_studio.py` (Hydrology tab); the per-tile overlay zeroes the precompute WP-findPath rivers and applies the paint as the carve geometry. Carver fixes 5a (lake water_y) + 5b (river water_y) handle the in-game appearance.

Most of S80 was failed iteration; the final pipeline is:

```
masks/hydro_region.png (user paint, 8192px id=2)
    │
    ▼ in core/hydro_region_overlay.py:_ensure_caches (ONCE globally at 8k)
    │   • skeletonize (no pruning — preserve length)
    │   • EDT half-width per painted cell
    │   • smooth: gaussian σ=4 → threshold 0.30 → gaussian σ=1 → threshold 0.45
    │   • erode 2 cells (≈12 MC blocks)
    │   • cache _river_edges_cache, _river_width_8k_cache,
    │           _paint_smooth_8k_cache, _paint_eroded_8k_cache
    │
    ▼ in core/hydro_region_overlay.py:_rasterize_river_edges_tile (PER TILE)
    │   • Bresenham edges from skeleton (smooth 50k curves)
    │   • bilinear-sample paint_eroded_8k → tile carve mask
    │   • bilinear-sample paint_smooth_8k → full mask (for mouth restore)
    │   • returns (centerline, width, full_smooth)
    │
    ▼ in core/hydro_region_overlay.py:apply_hydro_region_overlay (PER TILE)
    │   • ZERO precompute hydro_centerline / order / width / depth
    │   • write painted skeleton + width into masks
    │   • slope modifier: 1.0× flat → 0.6× steep
    │   • mouth restore: within 80 blocks of any sink → un-eroded full mask
    │   • shore bridge: basin ∩ near_river ∩ near_water → bridge cells
    │
    ▼ in core/river_carver_v2.py:carve_rivers
    │   • SKIP spline pickle (paint detected)
    │   • centerline_raw = order_u8 > 0 (only painted cells)
    │   • EDT distance to centerline
    │   • footprint = dist <= width (per-cell EDT-derived)
    │   • carve trench: depth_blocks per cell
    │   • water_y = nearest_avg + min(slope×width, 4) - 1   ← 5b
    │   • path-smooth water_y along centerline (gaussian σ=8)
    │   • force surface < water_y at every footprint cell
    │
    ▼ in run_pipeline.py
    │   • lake water_y = MIN(ceil(wl_float)) per terrain-intersection      ← 5a
    │   • blend river_water_y → lake_water_y over 8 cells at junctions
    │
    ▼ chunk_writer.py
        • paints water from sy+1 to river_water_y per cell
```

---

## Carver fixes (5a, 5b) — in production code

### 5a: lake water_y per-component MIN ceil
**File**: `run_pipeline.py:411-460`

```python
for lid in range(1, n_lakes + 1):
    lk = lake_labeled == lid
    if _wl_mc_float is not None and (_wl_vals := _wl_mc_float[lk][_wl_mc_float[lk] > -64]).size:
        lake_water = int(np.ceil(float(_wl_vals.min())))
    else:
        lake_water = int(pre_carve_y[lk].min()) + 1
    lake_water_levels[lid] = np.int16(lake_water)
    river_water_y[lk] = np.int16(lake_water)
```

**Why**: v23's `median(wl_vals)` + force `surface_y[lk] = lake_water - 1` carved basin walls into a flat shelf, looking like spillover. Per-pixel `ceil(wl_float)` (v25) was even worse — stepped water surface where wl_float varied ±1 within a connected component. `MIN(ceil)` per component gives one flat Y at the lowest spill. Cells where `terrain_int >= water_int` naturally don't paint water (chunk_writer's `abs_y > sy & abs_y <= rw` is empty), giving correct shoreline.

### 5b: river water_y bank-relative
**File**: `core/river_carver_v2.py:957-967`

```python
bank_lift = np.minimum(nearest_slope * nearest_width, 4.0).astype(np.float32)
water_y = nearest_avg + bank_lift - 1.0
```

**Why**: previous formula `nearest_avg - 1.5` gave water 1.5 blocks below smoothed centerline. For valleys with rising banks (centerline at Y=100, banks at Y=104), water at Y=98 → 4-block air gap visible at bank. Adding `slope×width` estimates bank rise → water tracks bank top → consistent ~1-block air gap regardless of slope.

### Carver gate fix
**File**: `core/river_carver_v2.py:254`

```python
_has_any_hydro = ((hydro_order is not None and hydro_order.max() > 0)
                  or (hydro_centerline is not None and hydro_centerline.max() > 0)
                  or (hydro_lake is not None and hydro_lake.max() > 0))
```

**Why**: lake-only tiles (no rivers) used to fall to the legacy carver which has no concept of `hydro_lake`. Tile (30,49) showed 0 lake water before this fix.

---

## Painted-river pipeline details

### Why painted rivers, not WP findPath?

User iteratively rejected WP findPath output:
1. Original output (RANDOMNESS=1000): rivers looked unnaturally straight
2. v25 (RANDOMNESS=2000, wavelength=8): rivers wandered to dead-ends, didn't reach lakes
3. v26 (RANDOMNESS=100, wavelength=16): subtle but didn't fix dead-end-at-basin-edge issue
4. Descent-based experiments (v3-v4): pure terrain flow accumulation produced 600k+ cells (every high cell drains) — way too many rivers

User decision: paint the rivers themselves. We pivoted to making `hydro_region.png` the canonical source.

### Globally-cached 8k post-processing

**File**: `core/hydro_region_overlay.py:_ensure_caches`

When `hydro_region.png` is loaded, `_ensure_caches` builds at 8k resolution:

| Cache | Computation |
|---|---|
| `_river_edges_cache` | Skeletonize paint mask, extract adjacency edges (no opening, no pruning) |
| `_river_width_8k_cache` | `distance_transform_edt(paint_mask)` — half-width per painted cell |
| `_paint_smooth_8k_cache` | gaussian σ=4 → threshold 0.30 → gaussian σ=1 → threshold 0.45 |
| `_paint_eroded_8k_cache` | `binary_erosion(_paint_smooth_8k_cache, iterations=2)` |
| `_lake_mask_cache` | Painted lakes (id=1) — currently unused for actual lake ID, just for paint-mode detection |

Doing this **at 8k once globally** eliminates tile-boundary truncation that earlier per-tile-erosion versions caused.

### Per-tile rasterization

**File**: `core/hydro_region_overlay.py:_rasterize_river_edges_tile`

Returns `(centerline_50k, width_50k, paint_full_smooth_50k)`:

1. **Bresenham edges**: each skeleton-adjacency edge in `_river_edges_cache` rendered as a Bresenham line at 50k. Smooth diagonals naturally; per-cell width interpolated from EDT at endpoints.
2. **Bilinear-sample eroded mask**: `_mc(_paint_eroded_8k_cache, coords, order=3)` produces the per-tile carve mask. Cubic interpolation = continuous derivative = no edge quantization.
3. **Bilinear-sample full smooth**: `_mc(_paint_smooth_8k_cache, coords, order=3)` for mouth restore.
4. **EDT width sampled smoothly**: `_mc(_river_width_8k_cache, coords, order=3)` × 8k→50k scale (~6.1).

### Apply-overlay post-process

**File**: `core/hydro_region_overlay.py:apply_hydro_region_overlay`

```python
# 1. Wipe precompute centerline/order/width/depth (paint = sole source)
if has_global_paint:
    for key in ("hydro_centerline", "hydro_order", "hydro_width", "hydro_depth"):
        masks[key][:] = 0

# 2. Slope modifier (steep narrows, flat widens)
slope_factor = 1.0 - 0.4 * slope_norm
per_cell_radius = per_cell_radius * slope_factor

# 3. Mouth restore — within 80 blocks of any sink, use full smooth mask
if sink_mask.any() and paint_full_smooth is not None:
    near_sink = distance_transform_edt(~sink_mask) < 80
    river_paint_modulated = river_paint | (paint_full_smooth & near_sink)

# 4. Shore bridge — within basin, connect painted river to underwater
near_river = binary_dilation(river_paint_modulated, iterations=30)
near_water = binary_dilation(underwater_local, iterations=30)
bridge = basin_local & near_river & near_water
river_paint_modulated = river_paint_modulated | bridge

# 5. Write to masks (centerline, order, width, depth)
masks["hydro_centerline"][river_paint_modulated] = 1
masks["hydro_order"][river_paint_modulated] = 2
masks["hydro_width"][river_paint] = per_cell_radius[river_paint]
masks["hydro_depth"][river_paint] = (per_cell_radius * 0.5 + 1.0)[river_paint]
```

`sink_mask` includes:
- Painted lakes (id=1) from `lake_paint`
- Ocean (`height ≤ 17050/65535`)
- Real terrain-intersection lakes (`hydro_lake>0 & height<lake_wl`)

---

## Override Studio Hydrology tab — what changed

**File**: `tools/override_studio.py:HydrologyPainterTab`

| Feature | Implementation |
|---|---|
| True 1px brush | Special-case `r==1` in `_on_paint` → `self.hyd[cy, cx] = id` (bypasses disc math which paints 5-cell plus at r=1) |
| Eraser tool | Virtual tool — canvas tool=brush, `_current_id=0` (paints pass-through). 🧽 button. |
| Brush hotkey | `B` via `QShortcut(QKeySequence("B"))` |
| Eraser hotkey | `E` via `QShortcut(QKeySequence("E"))` |
| Bucket fill | `_on_fill` — clicks on visible WP/precompute river overlay → flood-fills the connected channel with current paint id |
| Three overlay toggles | Independent checkboxes: Precompute (river.tif, orange-red), WP-1.7 (hydro_centerline.tif, vivid blue), Real lakes (terrain-intersection, cyan) |
| True ocean toggle | Renders `height ≤ 17050` as deep navy base layer |
| Gaea flow toggle | flow.tif log-tiered green: tributaries (>0.45), rivers (>0.65), trunks (>0.85) |
| Vivid yellow paint | `_refresh_paint_only`: id=2 → `[255,220,40,250]` |
| Clear all button | Wipes in-memory buffer + overwrites on-disk hydro_region.png with zeros |
| Cached pixmaps | `_rebuild_backdrop` pre-bakes biome backdrop + overlay backdrop QPixmaps; paint events skip rebuild (≈3× faster) |
| Line interpolation | `_on_paint` walks Bresenham line from `_last_paint_pt` to current; stamps disc at every step. Continuous strokes regardless of drag speed |
| DISPLAY_SIZE 2048 | Bumped from 1024 for finer paint precision |
| Binary mask loader | `_load_binary_mask_tif`: bilinear-read at 4× display + numpy max-pool (replaces invalid `Resampling.max` for `read()`) |

**Removed**: Active Category combo, Legend group, Pick eyedropper, lake/bank/wadi paint ids — all legacy noise. Hydrology painter is **river-only** (id=2).

---

## Diagnostic tools created

| Tool | Purpose |
|---|---|
| `tools/diag_painted_rivers.py` | Render the user's painted rivers in isolation against terrain hillshade |
| `tools/diag_basin_vs_ingame.py` | Side-by-side carved MCA vs raw basin extent — shows the orange/blue gap |
| `tools/diag_trace_real_lake.py` | 4-step trace: basin → contour → fill → real lake mask |
| `tools/diag_river_should_connect.py` | Highlights stranded river endpoints, draws Bresenham bridge to nearest underwater |
| `tools/diag_world_real_lakes.py` | World-scale topdown: ocean, real lakes, basin shore (orange), painted rivers |
| `tools/diag_world_corrected_rivers.py` | Option B: extend centerlines through orange basin to blue, plus subtle meander |
| `tools/diag_world_corrected_v2.py` | + connectivity invariants (forced inlets per lake, outlet steepest-descent) |
| `tools/diag_world_descent_rivers.py` | Pure steepest-descent rivers from peaks (per-pit recovery) — WAY too dense, abandoned |
| `tools/diag_world_descent_v2.py` | + per-pit Priority-Flood spillover detection |
| `tools/diag_world_descent_v3.py` | + global Priority-Flood DEM fill (Barnes 2014), cached at `memory/_cache_filled_dem_oceanonly.npy` |
| `tools/diag_world_descent_v4.py` | + flow accumulation thresholding (3-tier: streams ≥20, rivers ≥200, trunks ≥1000) — chosen approach for descent network, then ABANDONED in favor of paint-as-source |
| `tools/diag_preview_skeleton_at_tile.py` | Per-tile preview of overlay output (skeleton + Bresenham edges) |
| `tools/diag_v33_skeleton_and_carve.py` | Per-tile preview: centerline + predicted carve footprint (`dist ≤ width`) |
| `tools/diag_v34_compare.py` | Side-by-side A (skeleton-only) vs B (skeleton + paint floor) |
| `tools/diag_carve_preview_3x3.py` | 3×3 stitched carve preview around a center tile |
| `tools/wire_descent_to_centerline.py` | NOT USED — would have written descent rivers to hydro_centerline.tif. User pivoted to painted source instead |

---

## Iteration log (one-line summary per attempt)

| Version | Change | Result |
|---|---|---|
| v23 | Lake water_y = `ceil(median(wl_float))` + force surface_y down | Spillover: walls flattened to lake_water-1 |
| v24 | Carver gate: `_has_any_hydro` includes `hydro_lake.max()>0` | (30,49) lake-only tile finally got water |
| v25 | Lake water_y per-pixel `ceil(wl_float)`, no force-down | Stepped water surface where wl_float varied 1-2 cells across one component → spillover at the step |
| v25 | River water_y formula `-0.5 - sc - 1.0` → `-1.0 - sc` | Half-block raise; insufficient |
| v25 | Meander RANDOMNESS 1000→2000, wavelength 16→8 | Rivers wandered to dead-ends |
| v26 | Reverted RANDOMNESS to 100, wavelength 16 | Subtle, still dead-end at basin edge |
| v26 | Spillpoints fixed to use real lakes (already correct) | No change |
| v3-v4 | Global descent network with priority-flood pit-fill | 600k cells, way too many rivers — abandoned |
| v25 | Lake water_y = MIN(ceil) per component (5a final) | No spillover, single flat Y per lake |
| v25 | River water_y = nearest_avg + slope×width - 1 (5b final) | Bank-relative, consistent 1-block air gap |
| v27 | Disabled hydro_region.png (pivoting to descent) | Reverted in v28 |
| v28-v32 | Override studio rewrite: 1px brush, eraser, hotkeys, overlays, vivid paint, line interpolation | User can paint efficiently |
| v32 | Paint = SOLE source; zero precompute centerline before applying | But spline pickle still injected WP rivers — fixed by pickle-skip + rename |
| v33 | EDT-derived per-cell width from paint, slope+proximity modifiers | Width=12 cap too low; removed cap → mean width 33 |
| v33b | Option B: union NEAREST-cropped paint mask as floor | Fixed connectivity gaps but introduced 6-block staircase |
| v33c-d | Paint mask gaussian smoothing (σ=2 at 50k, then σ=1 at 8k) | Insufficient; staircase still visible |
| v34e | AGGRESSIVE smoothing: σ=4 at 8k + bicubic + σ=6 at 50k | Smooth but rivers "thick as fuck" |
| v34h | Erosion=12 + mouth-restore=80 | User: "less strained, perfect" |
| v34i | Per-tile erosion → GLOBAL 8k erosion → bilinear-sample | Tile-boundary breaks fixed |
| v34j | + shore-bridge for basin/underwater connection | Visible disconnect at (51,52) outlet not fully closed; bridge added 0 cells (gap was outside basin or > 30 blocks) |

---

## Open issues / next steps

1. **(51,52) lake outlet disconnect** — small visible gap between painted river endpoint and real lake water. v34j shore bridge didn't close it (probably outside basin or > 30-block reach). Investigation paused — user said "little annoying, not a big deal".

2. **Tributary-aware widening** — user's stated next idea: rivers should widen based on flow accumulation along the painted skeleton (small at headwaters, wide at mouth). Should be a POST-process after smoothing. Implementation sketch:
   - Skeletonize the carve mask
   - Find skeleton endpoints, junctions
   - DFS from each endpoint, accumulate cell count
   - Per-cell width modifier = `base + sqrt(flow_count) × scale`

3. **MCA render of (51,53) not yet validated in-game** — user wanted skeleton/preview iterations dialed in first. Awaiting their go-ahead to render and walk the painted result.

4. **Render budget for full-world** — once tile-render quality is approved, painted output spans 370k cells across the world. ~9,409 tiles × ~10-20min per tile = ~12-18 hour overnight render.

---

## Files touched this session

| File | Change |
|---|---|
| `run_pipeline.py:411-460` | 5a lake water_y MIN ceil per component |
| `core/river_carver_v2.py:254` | Gate includes hydro_lake |
| `core/river_carver_v2.py:545-572` | Spline pickle skip when paint exists |
| `core/river_carver_v2.py:957-967` | 5b river water_y bank-relative |
| `core/hydro_region_overlay.py` | Major rewrite: global 8k caches, REPLACE mode, EDT width, mouth restore, shore bridge |
| `core/region_overlay_smoothing.py:clean_painted_river_mask` | Endpoint pruning REMOVED |
| `core/hydrology_precompute.py` | Reverted meander tweaks back to baseline (RANDOMNESS=1000, wavelength=16) |
| `tools/override_studio.py` | Hydrology tab rewrite — 1px brush, eraser, hotkeys, overlays, vivid paint, line interp, cached pixmaps, channel-aware fill |
| `masks/river_splines.pkl` → `.disabled_v32_paint_only` | Renamed to prevent WP rivers leaking back |
| `tools/diag_*.py` | 14 new diag tools (see table above) |

---

## Backup / rollback path

To revert from painted-source back to WP findPath rivers:
1. `mv masks/river_splines.pkl.disabled_v32_paint_only masks/river_splines.pkl`
2. Either delete `masks/hydro_region.png` (paint mode auto-disables when cache is empty) OR
3. Click "Clear all painting" in override studio → wipes the file

The on-disk `hydro_centerline.tif` etc. were never modified; the WP-findPath geometry is preserved on disk and ready to use any time paint is removed.

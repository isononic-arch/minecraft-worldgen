# S81 River Handoff — `run_pipeline.py` Now Applies The Painted Overlay

*Session 81 (2026-05-04). Single critical fix + cleanup of the carver path.*

## TL;DR

**Every MCA render through `run_pipeline.py` was silently using the legacy WP-findPath rivers** from `hydro_centerline.tif`, not the painted overlay, because `apply_hydro_region_overlay` was only called in `tools/_pipeline_runner.py` (the validate-test-tile path). The production pipeline lacked the call. Adding one line after `read_tile` fixes it.

After the fix, water coverage on tile (51,53) went from **11% → 47.8%** — the painted rivers finally carve.

## The bug, in one diff

```python
# run_pipeline.py — added between read_tile (line 143) and assign_biomes (line 166)
masks = core_tile_stream.read_tile(...)

# ─── S81 fix ───
from core.hydro_region_overlay import apply_hydro_region_overlay
apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, w)
```

That's the load-bearing change. Without it, every painted-river session before S81 was operating on a preview tool that DID apply the overlay (so the user saw correct previews), and a render pipeline that DIDN'T (so the in-game result was always the legacy WP rivers).

## How it went undetected through S80

- `tools/diag_carve_preview_3x3.py` and `tools/validate_test_tile.py` both call `apply_hydro_region_overlay` directly — preview output looked correct.
- `tools/_pipeline_runner.py:168` had the call — the validate path also worked.
- `run_pipeline.py:_process_tile` is the actual production tile renderer, but it never got the call. S80 added the overlay code path but only wired it into the test-tile runner.

The fingerprint of the bug: in the MCA topdown, painted rivers appeared as thin 6-pixel staircased lines (the WP-findPath 1:8 → 50k NEAREST-upscale fingerprint), not the wide painted shapes. The user identified this as "legacy staircased path" before the fix landed.

## Other carver changes shipped in S81

The "no legacy code, smoothed gaussian only" directive drove these:

1. **`core/river_carver_v2.py:816-832`** — `_ORDER_TO_WIDTH = {1: 2.5, 2: 3.0, 3: 4.0, 4: 5.5, 5: 7.0}` deleted. Width comes from `hydro_width` (which apply_overlay sets per cell). Painted cells with no explicit width fall to 2.5-block default.

2. **`core/river_carver_v2.py:931-949`** — WP-guardrails tapered formula deleted. Replaced with flat-bottom: `new_y_f = nearest_avg - DEPTH` where `DEPTH = 4`. Carve depth uniform across the entire footprint instead of the old `(1-f) × (avg - depth × (1 - f×guard)) + f × surface_y` taper.

3. **`core/river_carver_v2.py:957`** — `footprint = river_full_mask & ~lake_mask & above_sea`. EDT halo extension via `dist_to_center <= nearest_width` removed. The footprint is now exactly the painted+smoothed area (no outward dilation).

4. **`core/river_carver_v2.py:~1090`** — Pass 2 edge-spillover guard removed. With flat-bottom carve, every footprint cell is uniformly lowered, so banks (cells outside footprint) keep their original elevation and water containment is automatic. The old guard was raising carved edge cells back to water_y level → ate ~half the carve width.

5. **`core/hydro_region_overlay.py:_ensure_caches`** — global smoothing changed:
   - `σ=4` → `σ=1.5` (preserves thin pixel-editor strokes ≥ 2 px)
   - `threshold=0.30` → `threshold=0.20`
   - Added `binary_closing(smooth | paint_mask, iterations=1)` to recover 1-pixel strokes
   - Erosion (2 iterations) removed entirely. `_paint_eroded_8k_cache = _paint_smooth_8k_cache` (the eroded cache is the smoothed mask itself).

6. **`core/hydro_region_overlay.py:apply_hydro_region_overlay`** — values written to `hydro_centerline / order / width / depth` are now tile_streamer-normalised (divided by 255 / 65535) so the carver's `_denorm_u8` round-trips cleanly. **Critical**: without this normalisation, `cl=1` round-tripped to 255 = the legacy carver's `braid_fill_mask = precomp_cl == 255` sentinel → every painted cell got gaussian-smoothed into a wide solid braid water blob. This was the symptom that originally surfaced before the missing-overlay-call bug was found.

7. **`run_pipeline.py:457`** — lake `water_y` uses `np.floor` instead of `np.ceil`. With wl=63.4: floor=63 (flush with basin rim), ceil=64 (1 above basin → water spills onto rim cells).

## HydrologyPixelEditor (`tools/override_studio.py`)

New `QDialog` for precision lake-river connection painting at native 8192 source resolution. Opens via "🔍 Open Lake Tile Editor (8192 native)" button at the bottom of the Hydrology-tab control panel.

- 1 brush pixel = 1 8k cell ≈ 6 MC blocks (vs 4× coarser in the main 2048-buffer tab)
- Backdrop shows ocean (deep navy, `height ≤ 17050`) + REAL lake basin underwater cells (`hydro_lake>0 & height<wl`, cyan). Critical: this is the *underwater* mask, not the basin polygon — early iteration showed the basin which extends way past the visible water.
- Brush + eraser, brush radius slider (1-20), B/E hotkeys, undo/redo (deque depth UNDO_LEVELS).
- Save writes 8192 PNG directly to `hydro_region.png` with NO upscale step. Backs up the previous file as `hydro_region.png.bak`. Reloads the parent tab's 2048 buffer so the main view stays in sync.

`BaseCanvas` was extended with a `canvas_size: int = DISPLAY_SIZE` parameter (defaults to 2048 to keep all the existing tabs working). The pixel editor passes `canvas_size=8192`.

## Open issue: in-game staircasing

After all the S81 fixes, painted rivers still show 6-pixel staircase steps in the MCA topdown. Cause: the painted mask is at 8k (1 cell = 6.1 MC blocks). The pipeline does:

1. `paint_mask` binary at 8k
2. gaussian σ=1.5 + threshold 0.20 + closing → smoother boundary at 8k (still pixel-quantised)
3. cubic-spline bilinear sample at 50k via `scipy.ndimage.map_coordinates(order=3)`
4. threshold `> 0.5` → binary at 50k

The threshold in step 4 produces hard edges that follow the 8k pixel grid contours, creating ~6-pixel staircase steps wherever the 8k boundary wasn't axis-aligned.

### Smoothing options for next session

| Option | Where | Cost | Pro | Con |
|---|---|---|---|---|
| (A) 50k post-bilinear gaussian | per-tile, in `_rasterize_river_edges_tile` | ~50ms/tile | Eliminates 8k staircase entirely | Per-tile cost adds up |
| (B) Signed-distance-field rendering | global, in `_ensure_caches` | One-time ~5s | Smooth contour by construction (best mathematically) | More code, SDF preserves negative distances which need handling |
| (C) Morphological close+open at 50k | per-tile after threshold | ~30ms/tile | Cheap, rounds staircase corners | Loses some thin-stroke fidelity |
| (D) Bicubic → bicubic-with-soft-threshold | in_rasterize | minor | Preserves more detail | Doesn't fix staircase root cause |

(A) is probably the right call for next session — applies the same kind of smoothing that already exists at 8k, just at the resolution where the user actually sees it. σ ≈ 2-3 at 50k would smooth ~6-pixel staircase steps without erasing brush detail.

### Why MCA staircasing is more visible than the preview suggests

The carve preview tool computes carve as `dist <= w[idx]` where w is the (now-zeroed) hydro_width — so the preview halo is essentially zero around a thin centerline. The actual carve uses `footprint = river_full_mask` (the smoothed paint area). So preview and MCA both follow the same underlying smoothed-paint shape, and both show the same staircase. The smoothing fix needs to apply to the smoothed mask itself.

## What this session deliberately leaves OUT

- Tributary widening (Hack's law `sqrt(upstream cells) × scale`) — the math is in `core/hydro_region_overlay.py:_compute_flow_accumulation` and gets cached as `_flow_accum_8k_cache`, but `apply_hydro_region_overlay` writes `per_cell_radius = 0` — the flow contribution is computed but not used. Re-enable by changing one line in apply_overlay if/when the user wants tributary-widened trunks again.
- Orphan endpoint bridges — the helper `_extend_orphan_endpoints` was added then removed during S81 iteration. User chose to paint lake-river connections manually via the new pixel editor instead.

## Files touched in S81

| File | Lines | Change |
|---|---|---|
| `run_pipeline.py` | 152-160, 457 | Added `apply_hydro_region_overlay` call; lake water_y `ceil → floor` |
| `core/river_carver_v2.py` | 816-832, 931-949, 957, ~1090 | Width from hydro_width; flat carve; footprint = river_full_mask; edge-spillover guard removed |
| `core/hydro_region_overlay.py` | `_ensure_caches`, `apply_hydro_region_overlay` | Smoothing tightened; values normalised by 255/65535; flow accumulation computed; orphan extension removed |
| `tools/override_studio.py` | new `HydrologyPixelEditor` class + button | 8192-native pixel editor |
| `tools/diag_carve_preview_3x3.py` | width × 255 in carve halo | Match the new normalised width values |

## Verification

Tile (51,53) MCA topdown: water cells **11,511 → 125,350** (10.9× increase). Lake at top renders correctly, river network covers the full painted footprint. Visual: [memory/s81_overlay_applied_mca_51_53.png](s81_overlay_applied_mca_51_53.png).

## Where to continue

1. **Smoothing in-game staircase** — pick option (A) above and add a per-tile σ=2.5 gaussian at 50k after the bilinear sample threshold.
2. **Tributary widening** — flip the `per_cell_radius = 0` back to the original `sqrt(flow) × _FLOW_WIDTH_SCALE` if the user wants trunks visibly wider than headwaters again.
3. **Render the full world** — every tile previously rendered before today's fix needs to be re-run; the entire 50k world output is currently the legacy WP rivers.

---

## STAIRCASE SAGA UPDATE (later in same session)

The in-game staircase fix went through 8+ iterations. Each tried a different angle on the boundary-smoothing problem. Documenting attempted fixes so we don't repeat them:

### v1: float-passthrough at 8k cache
Replaced binary cache `gaussian > threshold` with continuous float `gaussian(paint_mask)`. Threshold at 50k after bilinear. **Result:** boundary still followed 8k pixel grid contours (gaussian falloff aligns with paint pixel positions).

### v2: σ=2.5 50k post-bilinear gaussian
Added a second smoothing AT 50k after bilinear sample, to wash out 8k pixel grid before threshold. Slight improvement, **staircase still visible**. Bigger σ erases thin paint, so couldn't crank it higher.

### v3: SDF cache (signed distance, lightly smoothed)
Replaced gaussian-blur with `EDT(paint) - EDT(~paint)`. Smoothed σ=0.7. Threshold at -0.5 (slight expansion). **Result:** integer-pixel-distance lattice produced right-angle artifacts. Wider rivers due to threshold offset.

### v4: SDF σ=8
Heavy smoothing of the SDF integer-distance values. **Result:** thin paint (1-pixel strokes from pixel editor) had max SDF=0.5; σ=8 washed values below threshold 0 → most rivers ERASED. Bad.

### v5: lake-mechanism — subtract from cell's OWN surface_y
Carve depth still smooth, but subtract from `surface_out` (cell's original Gaea Y), NOT from `nearest_avg` (gaussian-smoothed). This preserves Gaea's natural ±1-2 block terrain noise across the carve. **Result:** boundary now has natural wiggle from terrain variation. Same physics as lakes.

### v6: heavy bank smooth incl. footprint
σ=16, zone=24, 3 passes. Allow raise inside footprint up to `water_y - 1` (preserves visible water). Lower freely outside. **Result:** smoother bank-to-bed transition. Threshold-mask boundary still visible at edges.

### v7 (current): continuous carve, no narrow footprint gate
**Carve applied EVERYWHERE `depth > 0.05`** (not just inside a strict footprint). Water_y_field set on the same broad zone (~22 blocks past visible paint). River_meta still strict at `depth ≥ 1` to prevent over-tagging banks. **Result:** boundary set by terrain intersection alone — best so far. Visible water boundary follows Gaea contours wiggling through the carve gradient. Still a faint 8k-lattice fingerprint in the carve depth field itself.

Implementation locations:
- `core/hydro_region_overlay.py:_ensure_caches` — SDF cache, light σ=1 smoothing
- `core/hydro_region_overlay.py:_rasterize_river_edges_tile` — bilinear sample SDF, σ=4 50k smoothing, sigmoid → continuous carve depth
- `core/hydro_region_overlay.py:apply_hydro_region_overlay` — write carve_clipped/255 into hydro_depth where carve > 0.02 (broad buffer)
- `core/river_carver_v2.py:7.6` — `depth_blocks_float = hydro_depth × 255`; `surface_out = original - depth`; footprint = depth > 0.05 (broad)
- `core/river_carver_v2.py:river_meta tagging` — uses `river_strict = depth ≥ 1.0` (not the broad footprint)

### v8 (LANDED): spline-fit river outline
The remaining staircase fingerprint came from the SDF being computed from a binary 8k pixel mask. Even after smoothing, the underlying signal had 8k-pixel structure baked in.

**Implemented:** vectorise the paint outline.
1. `skimage.measure.find_contours(paint_mask, 0.5)` extracts sub-pixel contour points at 8k.
2. For each contour, `scipy.interpolate.splprep([x, y], s=1.0×len, per=closed)` fits a periodic B-spline.
3. Spline sampled at ~10× contour length → dense 50k point cloud.
4. Per-tile distance via `scipy.spatial.cKDTree.query(tile_pts, k=1)` (analytical curve distance, no 8k lattice).
5. Inside/outside sign via union of `matplotlib.path.Path.contains_points` over each polygon.
6. Sigmoid `_CARVE_MAX_DEPTH / (1 + exp(-sdf / _CARVE_SOFTNESS))` → continuous carve depth (same downstream).

Code locations:
- `core/hydro_region_overlay.py:_build_spline_outline_50k` (~55 lines) — contour extraction + spline fit + dense sampling.
- `core/hydro_region_overlay.py:_ensure_caches` lines 341-371 — builds `_river_spline_pts_50k_cache`, `_river_spline_kdtree_cache`, `_river_spline_polygons_50k_cache` once globally.
- `core/hydro_region_overlay.py:_rasterize_river_edges_tile` lines 451-493 — `use_spline` branch performs per-tile cKDTree distance + Path.contains_points sign, then sigmoid carve depth.
- Falls back gracefully to the v7 EDT-cache SDF if `splprep` fails or paint has no contours.

Smoothness param `s = 1.0 × len(contour)` (mid range = WorldEdit smooth-brush feel). To tune later: `_build_spline_outline_50k(..., smoothness_factor=X)`. low s (~0.1×N) = follows paint exactly; high s (~10×N) = aggressive smoothing.

### v8.1 (LANDED + USER-VALIDATED): smoothness 3 + choke + WP-style periodic meander

User feedback after v8.1 render: **"newest render looks great. Beautiful."** This is the milestone state.

**Changes vs v8 baseline:**

1. **Spline smoothness 1.0 → 3.0** in `_build_spline_outline_50k` call (line 466 of `core/hydro_region_overlay.py`). Rounder corners, contour micro-jaggies washed out.

2. **Inward bias choke.** New constant `_CARVE_INWARD_BIAS = 5.0` in `_rasterize_river_edges_tile`. Sigmoid centre shifted inward by 5 blocks → carved water zone shrinks below painted footprint. Pairs with `_CARVE_SOFTNESS = 4.0 → 2.5` (sharper sigmoid → bias actually bites instead of being eaten by the soft tail).

   Formula: `carve_depth = MAX / (1 + exp(-(sdf - bias) / softness))`

   Effect at current values: a 12-block painted river carves to a ~6-7 block visible water channel. To re-tune, see CLAUDE.md "Tunables for next iteration".

3. **WP river_script1.7-style periodic meander.** New params on `_build_spline_outline_50k`:
   - `periodic=True` (default) — sin wave along arclength with phase modulated by low-freq simplex.
   - `periodic_amp_blocks=6.0`, `periodic_wavelength_blocks=140` — meander cycle amplitude/wavelength.
   - `phase_distortion_amp_blocks=350`, `phase_distortion_wavelength_blocks=800` — low-freq simplex perturbs the sin's phase across ~800-block stretches → not a perfect sinusoid, breaks the tin-spaghetti look.
   - `micro_amp_blocks=1.0`, `micro_wavelength_blocks=30` — high-freq simplex jitter on bank.
   - `periodic=False` falls back to raw two-octave noise (no sin).

   Mechanism per spline sample i along the spline:
   ```
   arclen[i]   = cumulative arc-length in MC blocks from start of contour
   phase_noise = simplex_low(world_x[i] / 800, world_y[i] / 800)   # ±1
   micro_noise = simplex_high(world_x[i] / 30,  world_y[i] / 30)   # ±1
   phase_arg   = (arclen[i] + phase_noise × 350) × 2π / 140
   disp_blocks = sin(phase_arg) × 6 + micro_noise × 1
   ```
   Then `disp_blocks` is converted to 8k px (`/ scale`) and pushed perpendicular to the spline tangent (computed via `np.roll` finite-difference for closed loops). Both banks of a narrow channel see correlated displacement → channel meanders rather than fattens/thins.

   **Why this is borrowed from WP river_script1.7 not invented from scratch:** WP's `repeatableRandom(x, y)` (lines 846-869 of `river_script1.7.js`) uses two-octave simplex modulating sin() phase as a Dijkstra cost-function bias during pathfinding. We adapt the same noise theory but apply it as geometric perpendicular displacement (since we don't have a pathfinder — we paint outlines and spline-fit). See CLAUDE.md "Report on WP river_script1.7 meander logic" if you need the line-by-line audit.

   **WP coefficients borrowed:** ~7:1 octave amp ratio (we use 6:1), low-freq wavelength much greater than meander wavelength (WP: 1000 vs 188; us: 800 vs 140). High-freq amp comparable to bank-jitter scale.

   **What we did NOT copy:** WP runs noise through pathfinding, not as displacement. Their result has Dijkstra-grid-quantisation artifacts that they smooth out at width-time. Our spline-displacement avoids that entirely.

**Validation:** `memory/s81_v8_meander_compare_51_53.png` (left = choke-only baseline, right = choke + periodic meander). Bank undulation visible especially on top-right peninsula and main right-bank arc. In-world the 6-block amplitude reads as natural lazy bends because each 140-block wavelength is a real first-person walk.

**Tunables (all in `core/hydro_region_overlay.py`):**

| What | Constant / arg | Current | Higher = | Lower = |
|---|---|---|---|---|
| River width | `_CARVE_INWARD_BIAS` | 5.0 | thinner | wider |
| Bank softness | `_CARVE_SOFTNESS` | 2.5 | softer terrain transition | sharper, harder edge (≤1.5 → staircase returns) |
| Corner roundness | `smoothness_factor` (call site) | 3.0 | rounder | tighter to paint |
| Meander amplitude | `periodic_amp_blocks` | 6.0 | taller bends | flatter |
| Meander wavelength | `periodic_wavelength_blocks` | 140 | wider lazy bends | tighter cycles |
| Phase wobble | `phase_distortion_amp_blocks` | 350 | more random | more uniform sinusoid |
| Bank irregularity | `micro_amp_blocks` | 1.0 | noisier banks | smoother banks |
| Periodic vs raw | `periodic` (kw) | True | (WP-style sin) | False = raw 2-octave noise |

### Other in-progress fixes still in code

- **Steppable shore lip** (`core/river_carver_v2.py:7.6c`): 1-cell ring at footprint boundary forced to `water_y + 1`. Wadeable hop-out from water.
- **Bank smooth-brush** (`core/river_carver_v2.py:7.6b`): heavy gaussian σ=16, zone=24, 3 passes. Now includes footprint cells with water-preservation cap (raise capped at `water_y - 1`).
- **U-shape vs flat-bottom carve**: replaced WP guardrails formula with sigmoid carve depth from terrain-intersection field. WP-faithful in shape (deeper at center, tapers at banks) but never blends to original_height (preserves wide water).

### Files touched in S81 (cumulative)

- `run_pipeline.py:152-160` — apply_hydro_region_overlay call (THE critical fix)
- `run_pipeline.py:457` — np.floor for lake water_y
- `core/hydro_region_overlay.py` — major rewrite of `_ensure_caches` + `_rasterize_river_edges_tile` + `apply_hydro_region_overlay` (SDF cache, sigmoid, broad water_y zone)
- `core/river_carver_v2.py` — replaced WP-guardrails U-shape with terrain-intersection carve from precomputed depth field; bank smooth + steppable shore + ORDER_TO_WIDTH deletion
- `tools/override_studio.py` — HydrologyPixelEditor (8192-native paint dialog)
- `tools/diag_carve_preview_3x3.py` — width × 255 for normalised reading

### MCA render artifacts (memory/)

Sequential progress (latest at top):
- `s81_v8_meander_periodic_51_53.png` — **v8.1 LANDED + USER-VALIDATED.** Spline-fit + smoothness=3 + bias=5 + softness=2.5 + WP-style periodic meander (amp=6, λ=140, phase=350/800, micro=1/30). User said "Beautiful."
- `s81_v8_meander_compare_51_53.png` — side-by-side: choke-only vs choke+meander
- `s81_v8_choked_smooth3_bias5_soft25_51_53.png` — v8 + smoothness=3 + aggressive choke (no meander)
- `s81_v8_tuned_smooth3_bias25_51_53.png` — v8 + smoothness=3 + mid choke (no meander)
- `s81_v8_3way_compare_51_53.png` — orig spline / mid-tune / aggressive-choke (no meander)
- `s81_v8_orig_vs_tuned_51_53.png` — v8 baseline vs first tune
- `s81_v8_spline_51_53_topdown.png` — v8 baseline (smoothness=1, no choke, no meander)
- `s81_v7_vs_v8_compare_51_53.png` — v7 EDT-SDF vs v8 spline-SDF
- `s81_continuous_carve_mca_51_53.png` — v7 continuous carve, terrain-intersection
- `s81_lake_mechanism_mca_51_53.png` — v5 cell-own surface_y subtraction
- `s81_full_smooth_mca_51_53.png` — v6 heavy bank smooth incl. footprint
- `s81_heavy_smooth_mca_51_53.png` — v6 σ=16 zone=24 3 passes
- `s81_50k_smooth_mca_51_53.png` — v2 σ=2.5 post-bilinear gaussian
- `s81_sdf_sigma8_mca_51_53.png` — v4 SDF heavy smooth (thin rivers gone)
- `s81_sdf_mca_51_53.png` — v3 SDF light smooth
- `s81_overlay_applied_mca_51_53.png` — first render after the missing-overlay-call fix (water 11% → 47.8%)
- `s81_smoothonly_mca_51_53.png` — pre-overlay-fix (legacy WP rivers, narrow staircased)

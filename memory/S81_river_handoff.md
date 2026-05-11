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

---

## S81 v8.2 → v8.14 PROGRESSION (post-"Beautiful" iteration log)

After v8.1 landed and user said "Beautiful," several follow-up iterations addressed remaining issues. Most attempts hit walls and were reverted. Final state: v8.14 (user verdict: "Pass working great").

### v8.2 — Per-branch arclength water_y (REVERTED)

Replaced `nearest_avg + bank_lift - 1` formula with per-branch arclength + lake snap. BFS along skeleton, water_y monotone-stepped from downstream to upstream. Lake snap forced river endpoint water_y to lake's water level if within `_SNAP_RADIUS=6` cells.

**Why it broke:** at (51,53), the painted lake was at high elevation (Y=89) with terrain ~Y=70-80 around the river endpoint. The snap fired because the lake was within radius, raising the entire river branch's water_y to 89 → 20-block water columns at the junction + flat rivers (no descent because the cap fixed water_y to a constant).

Reverted to v8.1 formula.

### v8.3 Step A — Skeleton-EDT for `nearest_idx` propagation (LANDED)

The cross-section variance (14k uneven cells per `tools/diag_water_y_dump.py`) traced to: `_edt_g(~river_full_mask)` runs EDT on the WIDE painted footprint. Inside-footprint cells get `nearest_idx = themselves`, inheriting per-cell `nearest_avg + bank_lift - 1` values. Cross-sections weren't uniform.

Fix (Step A): change EDT source to `~skeletonize(river_full_mask)`. Now every footprint cell maps to a single 1-cell-wide centerline cell → `nearest_avg / slope / width` uniform per Voronoi region → cross-section uniformity.

Result: 14k → 958 uneven cells (-93%). No regressions. Diagnostic: `memory/diag_water_y_v83a/`.

### v8.4 — Deeper trough + river-paint-wins-on-overlap + veg-kill (LANDED)

Three landed fixes:
1. `_CARVE_MAX_DEPTH = 4 → 6` in `core/hydro_region_overlay.py`. User wanted deeper troughs.
2. `lake_paint = (hr_arr == 1) & ~paint_mask` in `_ensure_caches` — at painted river-AND-lake overlap, river paint wins. Stops painted-river cells inside painted-lake area from being treated as lake water.
3. Veg-kill on water-zone + 1-cell buffer in `core/chunk_writer.py:build_column_array` — drops grass/bushes from cells where `river_water_y > 0` (cell is in water zone, even if no actual water filled the column at sy+1). Eliminates "grass floating at river edges."

### v8.5 — Smoothstep + Step B + lake wall + underwater grass swap (PARTIAL REVERT)

Bundled four changes; only the smoothstep + underwater grass swap survived.

1. **Smoothstep replaces sigmoid** (KEPT): `_t_norm = clip((sdf - (bias - softness))/softness, 0, 1); carve_depth = MAX × _t_norm² × (3 - 2·_t_norm)`. Plateau at `sdf >= bias` gives explicit MAX_DEPTH for most of the river width. v8.1's sigmoid had no plateau — only the very center reached MAX. User wanted "deeper across most of width."
2. **Step B: `_grav_labeled` from skeleton** (REVERTED at v8.6): caused triangular water columns at confluences. At Y-junctions, the skeleton has degree-3 cells in a single connected component. The 1D path-smoothing via `argsort(-dist_from_ocean)` interleaved cells from converging branches → smoothed water_y mixed independent flow paths.
3. **Lake containment wall** (REPLACED at v8.6): hardcoded 1-cell ring around lake_mask raised to lake_water_y. Worked but inflexible. Replaced by generic escape-fix.
4. **Underwater grass→dirt swap** (KEPT) in `core/chunk_writer.py`: surface_blk swapped to dirt where `river_water_y > surface_y`. No more green grass blocks visible underwater.

### v8.6 — WP-style escape-fix pass + tuned smoothstep (LANDED)

Replaced v8.5's hardcoded lake wall with the WP river_script1.7 "fix water escaping" iterative pass (lines 688-737 of `river_script1.7.js`). Generic invariant: at convergence, no water cell has a neighbor with surface_y < that water cell's water_y.

Implementation in `run_pipeline.py` after the river-lake blend:
```python
for _escape_iter in range(5):
    _nbr_max_wy = maximum_filter(_water_y_positive, size=3)
    _leak_cells = (
        (surface_y < _nbr_max_wy)
        & (_nbr_max_wy > SEA_LEVEL)
        & (river_water_y < 0)  # land cells only
    )
    if not _leak_cells.any(): break
    surface_y[_leak_cells] = _nbr_max_wy[_leak_cells]
```

Handles lake containment, river bank leaks, and lake-river junction containment in one consistent pass. Only LAND cells raised — water cells are left alone (MC fluid physics handles cascade between different water levels naturally).

Also tuned smoothstep: `_CARVE_INWARD_BIAS = 5 → 4`, `_CARVE_SOFTNESS = 2.5 → 3`. Compromise between v8.4's narrow water and v8.5's overly wide.

Reverted v8.5's Step B in carver — `_grav_labeled = _label_grav(river_full_mask)` (back on wide footprint) eliminated triangle artifacts at confluences.

### v8.7 — Stepped berm widening (REPLACED at v8.8)

Added 3-iteration stepped widen after escape-fix: cells at distance 1/2/3 from water raised to (water_y - 1)/(water_y - 2)/(water_y - 3). Created a 3-cell-thick berm slope from water level outward.

Replaced by v8.8 EDT-based version (smoother, single-pass).

### v8.8 — EDT-based smooth-slope berm + escape-fix gated to land only (LANDED)

`_dist_from_water` from EDT(~water_mask). For each land cell within `BERM_RADIUS = 8`:
```python
target_y = nearest_water_y - dist_from_water
need_raise = (surface_y < target_y) & (river_water_y < 0)
surface_y[need_raise] = target_y[need_raise]
```
Single-pass EDT replaces v8.7's iterative stepped widen. 1-block-per-cell falloff out to 8 cells from any water cell. Only RAISES (never lowers), preserving natural cliffs.

Escape-fix narrowed: `(river_water_y < 0)` only. Water cells with lower water_y than neighbors are LEFT ALONE — MC fluid physics cascades lake water down to river naturally. Earlier broader condition raised the surface of water cells too, creating a dam at lake-river junctions.

### v8.9 — Slope shift attempt (REVERTED slope, KEPT density)

Tried `target = nearest_water_y - max(dist - 1, 0)` so wall (dist=1) stayed at water_y AND berm descended 1/cell from dist=2 onward (matches escape-fix wall). Theoretically smoother visual.

**Why it broke:** rivers in the middle of the 3×3 didn't connect to the lake. The shifted slope put dist=2 at water_y - 1 (was water_y - 2 in v8.8) — 1 block higher overall berm. This subtle change combined with the escape-fix raise patterns caused some river cells to no longer be visible.

Reverted slope shift to v8.8 `target = water_y - dist`. Kept the BIRCH_FOREST density bump (0.30 → 0.36) which was bundled with v8.9.

### v8.10 — Lake water_y rim cap (REVERTED)

Tried "Option A" — for each painted lake cluster, find natural rim max in 2-cell ring outside, cap `lake_water_y = min(precompute_wl, natural_rim_max)`. Goal: kill the perched-lake stamp aesthetic by lowering high-elevation lakes to natural surrounding terrain.

**Why it broke:** painted-river cells inside precompute basins get tagged `CHAN_LAKE` by the carver (because the carver loads `hydro_lake_wl.tif` from disk at line 339, sees terrain < precompute_water_y, sets CHAN_LAKE). When `river_water_y[CHAN_LAKE cells]` was set to the new low capped lake_water, painted-river cells dropped below their carved surface → dry. Right side of (52,53) tile lost all river visibility.

Reverted.

### v8.11 — Paint-aware basin zeroing in carver (REVERTED)

Tried zeroing `pad_wl` at painted-river cells in the carver's section 3 disk-load path. So the basin classification skips painted-river cells, they get treated as plain CHAN_RIVER.

**Why it broke:** the river formula `nearest_avg + bank_lift - 1` gave too-low water_y for painted rivers at high terrain. With `nearest_avg` smoothed from carved (low) surface, water_y ended up at SEA_LEVEL → no visible water. The CHAN_LAKE classification was actually KEEPING water visible (by giving cells the basin's high water_y) — removing it broke that.

Reverted.

### v8.12 — `BLEND_DIST = 8 → 24` (LANDED) — east inlet connectivity fix

The simple working fix: bump the existing river-lake water-y blend zone from 8 to 24 cells. Lake at Y=89 + river at Y=70 = 19-block elevation gap. The 8-cell blend was too short to bridge it; beyond 8 cells river_water_y dropped back to ~Y=70, vertically disconnected from the lake.

`BLEND_DIST = 24` covers the full 19-block gap with ~1.25 blocks/cell linear interpolation. Lake-river east-inlet cascade is now visible AND continuous from the lake at Y=89 down to natural river elevation.

Single-line change (`BLEND_DIST = 8 → 24` in `run_pipeline.py:522`). Also kept escape-fix + EDT berm to handle bank containment for the now-higher river water_y in the blend zone.

### v8.13 — Bank smooth-brush footprint-only (LANDED)

User feedback: "trench wall higher than the soft valley around it." Diagnosis: bank smooth-brush in `core/river_carver_v2.py:7.6b` was operating on a 24-cell-wide zone INCLUDING bank cells. Gaussian sigma=16 reached into the carved (low) trench and pulled bank cells DOWN, creating an artificial valley dip. EDT berm only reaches 8 cells, so beyond 8 cells the smoothed-down land stayed below natural — wall + berm sat above this valley dip.

Fix: smooth-brush zone limited to FOOTPRINT cells only. Bank cells stay at natural elevation. Smoothing inside trench still smooths the carve-floor taper pattern. Bank shaping now entirely the responsibility of escape-fix + EDT berm (which have correct slope back to natural terrain since natural is no longer pulled-down).

Removed `BANK_ZONE = 24` constant + `_edt_bank` import + `dist_from_fp` computation. Simplified to `in_zone = footprint & above_sea & ~lake_mask`.

### v8.14 — Final water-level cleanup pass (LANDED — user "Pass working great")

User-requested rule: in rivers, water_y should NEVER be at or above the adjacent NON-CARVED (pre_carve_y) bank elevation.

Final pass in `run_pipeline.py` AFTER escape-fix + EDT berm:
```python
HIGH = np.int16(10000)
masked_bank = np.where(river_cells, HIGH, pre_carve_y).astype(np.int16)
min_bank_3x3 = minimum_filter(masked_bank, size=3)
edge_with_bank = river_cells & (min_bank_3x3 < HIGH//2)
# EDT propagate cap into wide-river interior
_, edge_idx = distance_transform_edt(~edge_with_bank, return_indices=True)
propagated_cap = (min_bank_3x3 - 1)[edge_idx[0], edge_idx[1]]
# EXCEPTION: preserve BLEND-affected cells (within 24 of lake)
if lake_mask.any():
    is_blend_cell = distance_transform_edt(~lake_mask) <= 24
else:
    is_blend_cell = zeros
# Lower water_y where above cap AND outside blend zone
too_high = river_cells & (river_water_y > propagated_cap) & ~is_blend_cell
river_water_y[too_high] = propagated_cap[too_high]
```

Effects:
- River edge cells: cap at `min(adjacent natural bank) - 1`
- Wide-river interior cells: inherit cap from nearest edge cell via EDT propagation → cross-section uniform
- BLEND-protected cells (within 24 of any lake): UNCAPPED, keep their lake-blended water_y → cascade preserved at lake-river junctions
- Lakes (CHAN_LAKE): UNTOUCHED — only CHAN_RIVER + CHAN_STREAM affected
- Result: no more "perched water above natural ground" / "1-cell wall sticking up out of land" artifacts

### Files touched in S81 v8.2 → v8.14 (cumulative since v8.1)

- `core/hydro_region_overlay.py` — v8.4 MAX_DEPTH 4→6 + lake_paint fix; v8.5 smoothstep replaces sigmoid; v8.6 bias 5→4 + softness 2.5→3
- `core/river_carver_v2.py` — v8.3 skeleton-EDT (Step A); v8.5 Step B then REVERTED v8.6; v8.13 smooth-brush footprint-only
- `core/chunk_writer.py` — v8.4 veg-kill water-zone + buffer; v8.5 underwater grass→dirt swap
- `core/schematic_placement.py` — v8.9 BIRCH_FOREST density 0.30 → 0.36
- `run_pipeline.py` — v8.6 WP-style iterative escape-fix; v8.8 EDT-based berm + escape-fix gated to land only; v8.12 BLEND_DIST 8→24; v8.14 final water-level cleanup pass with BLEND-cell exception
- `tools/diag_water_y_dump.py` — NEW diagnostic for water_y per-cell dump

### Render artifacts (memory/) — final state

- `s81_v812_3x3_stitched.png` — first 3×3 with east-inlet connected via BLEND_DIST=24
- `s81_v813_3x3_stitched.png` — bank smooth-brush footprint-only validation
- `s81_v814_50_53.png` — final v8.14 cap validation
- `s81_v89_vs_v810_3x3.png` — slope shift bug evidence (rivers missing in middle)
- Various v8.x_compare images for iteration breadcrumbs

### Tunables summary (final v8.14 state)

| Constant | File | Current | Notes |
|---|---|---|---|
| `_CARVE_MAX_DEPTH` | hydro_region_overlay.py | 6.0 | Plateau depth |
| `_CARVE_INWARD_BIAS` | hydro_region_overlay.py | 4.0 | Choke amount (cells inward from paint edge before plateau) |
| `_CARVE_SOFTNESS` | hydro_region_overlay.py | 3.0 | Smoothstep transition zone width |
| `smoothness_factor` | hydro_region_overlay.py:_build_spline_outline_50k call | 3.0 | Spline corner roundness |
| `periodic_amp_blocks` | hydro_region_overlay.py | 6.0 | Meander amplitude |
| `periodic_wavelength_blocks` | hydro_region_overlay.py | 140 | Meander cycle length |
| `phase_distortion_amp_blocks` | hydro_region_overlay.py | 350 | Phase wobble strength |
| `phase_distortion_wavelength_blocks` | hydro_region_overlay.py | 800 | Phase wobble scale |
| `micro_amp_blocks` | hydro_region_overlay.py | 1.0 | Bank irregularity |
| `micro_wavelength_blocks` | hydro_region_overlay.py | 30 | Bank irregularity scale |
| `BLEND_DIST` | run_pipeline.py | 24 | Lake-river water_y blend distance |
| `_BLEND_PROTECT_DIST` | run_pipeline.py (v8.14 cap) | 24 | Match BLEND_DIST — protected from cap |
| `BERM_RADIUS` | run_pipeline.py | 8 | EDT berm extent from water |
| `BANK_SIGMA` | river_carver_v2.py | 16.0 | Smooth-brush gaussian sigma (footprint-only) |
| `BANK_PASSES` | river_carver_v2.py | 3 | Smooth-brush iterations |
| `BIRCH_FOREST` density | schematic_placement.py | 0.36 | Tree density (was 0.30) |

### Open backlog (DEFERRED — for full-world 50k regen prep)

1. **Tile-boundary line artifacts** — per-tile filters can't see across boundaries. Visible at tile seams. Fix: pad surface_y + river_water_y with 32-48 cells of neighbor data before escape-fix + berm + smooth-brush. ~70 lines in `run_pipeline.py`. **Test on 2 tiles before full-world.**
2. **Spline cache disk persistence** — currently rebuilds on every fresh process (~10-15 min). For 9409 tiles × 2-4 workers = ~3000 process boots = ~750 hours redundant work. Pickle caches with paint+code+param hash key. **Test on 2 tiles before full-world.** Risk: stale cache mid-iteration — disable during active spline param tuning.
3. **OOM with --threads ≥3** — each worker holds 500-800MB. Use `--threads 2` for 16GB-class machines.
4. **Lake water_y still pulled from precompute `hydro_lake_wl`** — works but tightly coupled to whatever the precompute decided. If you ever want paint-driven lake water levels, would need plumbing. Low priority.
5. **Hand-painted boundary id=1 (lake) ↔ id=2 (river)** — overlap case handled (river wins). Adjacent-only case probably works but never tested explicitly at boundary cells.

### What's not in the docs but was learned

- `pre_carve_y = surface_y.copy()` captured at line 203 of `run_pipeline.py` BEFORE `carve_rivers` is the "natural terrain" reference. Used by v8.14 cap. Don't forget this line — without it, the cap formula uses post-carve surface and gives wrong values.
- `CHAN_STREAM = np.uint8(1)` is defined inside `if lake_mask.any():` block (line 491). v8.14's cap pass must define `_CHAN_STREAM_CAP` locally because it runs OUTSIDE that conditional and would UnboundLocalError on lake-less tiles.
- The carver loads `hydro_lake_wl.tif` from disk at line 339 (section 3 padded basin handling). Modifying the masks dict in `apply_hydro_region_overlay` does NOT affect this disk-load path. Both paths must be modified consistently when changing basin classification logic.
- `River_meta == CHAN_LAKE` is set in carver section 3 for cells where painted-or-precompute basin AND `terrain_y < water_y`. Painted-river cells overlapping precompute basins inherit this label — workaround in v8.4 (river paint wins lake_paint subtraction in `_ensure_caches`) only affects the painted lake mask, NOT the precompute basin loaded from disk.

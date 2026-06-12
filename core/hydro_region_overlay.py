"""
core/hydro_region_overlay.py — S70

Applies the user-painted ``masks/hydro_region.png`` overlay onto per-tile
hydrology masks read from disk.  This lets the Hydrology Paint tab in
``tools/override_studio.py`` drive actual river / lake placement at tile
render time without touching the pipeline-generated hydro_* TIFs on disk.

Painted categories in hydro_region.png (HYDRO_REGIONS in override_studio.py):
    0 = pass-through (no override)
    1 = lake / oasis         → force lake + default depth
    2 = river / stream       → force centerline + default order + width + depth
    3 = river bank (moist)   → (stub; future moisture boost, no biome change)
    4 = dry channel / wadi   → (stub; future carve-without-water)

File layout: hydro_region.png is 8192×8192 uint8, covering the same world
extent as override_final.png.  For a 50 000×50 000 world (TILE_SIZE=512),
each tile spans ~84 source pixels.  We crop + NEAREST-upscale to tile size.

Safe to call when the file is absent — it simply returns without mutating.
"""
from __future__ import annotations

from pathlib import Path
import numpy as np

# World constants — keep in sync with core/tile_streamer / run_pipeline.
_WORLD_PX = 50_000
_REGION_PX = 8_192

# Defaults applied to painted pixels — matches the mid-order river settings.
_RIVER_ORDER_MIN = 2
_RIVER_WIDTH_MIN = 3
_RIVER_DEPTH_MIN = 2
_LAKE_ID_DEFAULT = 9999         # any nonzero ID works; 9999 avoids collision
_LAKE_DEPTH_MIN  = 2

# ── Tributary-aware widening (S81) ──
# Per-cell additive width from upstream flow accumulation. Width contribution
# is sqrt(upstream_8k_cells) × _FLOW_WIDTH_SCALE, in MC blocks at 50k. With
# scale=0.30: a small tributary (100 upstream cells) adds ~3 blocks; a major
# trunk (10k cells) adds ~30 blocks. Stacks ADDITIVELY on EDT-derived width
# from paint thickness.
_FLOW_WIDTH_SCALE = 0.30

# ── S93 headwater inset ──
# Painted lines have a hard width floor (1 paint px ≈ 6 blocks at 50k):
# every headwater rendered 9-11 blocks wide regardless of accumulation.
# Fix is GEOMETRIC: inset the channel SDF by (1 - t) × local paint
# half-width, where t comes from skeleton flow accumulation. Width then
# scales LINEARLY with t (tips ≈ 2-3-block creeks), and depth couples
# naturally because the depth curve evaluates the inset SDF (a quarter-
# width creek is proportionally shallower — Leopold-ish w ∝ Q^~0.5).
# Applied identically at BOTH SDF consumers (8k bed + 50k carve) so bed
# and footprint cannot disagree (the S92 v4 lesson).
#   t = clip((accum / _HW_A_REF) ** _HW_EXP, _HW_T_MIN, 1.0)
# S93 STATUS: DISABLED pending the carver width-map session. Two gates
# produced internally inconsistent width/depth responses ((62,61): gate 1
# narrowed width AND collapsed depth; gate 2 with a verified-identical
# field left width untouched while depth stayed collapsed) — rendered
# channel width is NOT a single-lever function of this SDF (centerline +
# EDT width radius + polygon SDF + carve thresholds + bed override all
# contribute). Flag stays False until that pipeline is mapped end-to-end;
# the field build + both subtraction sites below are sound and reusable.
_HW_INSET_ENABLED = False
_HW_A_REF = 150.0     # accumulation (8k skeleton cells) at which t = 1
_HW_EXP = 0.45
_HW_T_MIN = 0.22      # width floor factor (tips keep ~1/4 paint width)
_BLOCKS_PER_8K = 50000.0 / 8192.0
# uint8 inset field (blocks), built in _ensure_caches alongside the bed.
_hw_inset_8k_cache: "np.ndarray | None" = None


# ---------------------------------------------------------------------------
# Global skeleton cache
#
# Rivers (id=2) need SMOOTH 50k rasterization — simple NEAREST upsample of
# the 8192-resolution paint produces a staircase that the distance-transform
# channel widener amplifies into 20-block right-angle chunks.  Gaussian
# smoothing of the upsampled mask doesn't sharpen enough either.
#
# Fix: skeletonize the painted river mask ONCE at 8192, extract adjacency
# edges, then per-tile rasterize each edge as a Bresenham line at tile
# resolution.  A 1-pixel 8192 diagonal becomes a ~6-pixel 50k line per
# skeleton step — properly smooth instead of blocky.  Lakes (id=1) stay on
# NEAREST crop since filled areas don't show staircasing.
# ---------------------------------------------------------------------------

_river_edges_cache: list[tuple[int, int, int, int]] | None = None
_river_width_8k_cache: np.ndarray | None = None  # EDT half-width per painted cell
# v34i: globally-smoothed + eroded paint mask at 8k. All shape post-
# processing happens at 8k (NOT per-tile) to avoid boundary breaks
# where erosion treats outside-tile as no-paint.
_paint_smooth_8k_cache: np.ndarray | None = None    # smoothed FLOAT32 [0,1]
_paint_eroded_8k_cache: np.ndarray | None = None    # alias of smooth (no erosion in S81)
_lake_mask_cache: np.ndarray | None = None
# S81: per-painted-cell upstream flow accumulation (8k cell count). Used to
# add Hack's-law width on top of the EDT-derived paint width. Direction is
# inferred from terrain elevation along the skeleton.
_flow_accum_8k_cache: np.ndarray | None = None

# S83 v8: carve tunables (module-level so _ensure_caches can use them in
# global bed precompute and _rasterize_river_edges_tile uses same values).
_CARVE_MAX_DEPTH = 6.0        # S83 v17: now only used as compatibility constant
                              # (legacy print stat); the actual carve uses
                              # _DEPTH_POWER_SCALE * sdf^_DEPTH_POWER_EXPONENT
                              # (no plateau, no cap, lake-bowl-style profile)
_CARVE_SOFTNESS = 3.0         # S83 v17: kept for legacy code paths but unused by new power-curve carve
_CARVE_INWARD_BIAS = 4.0      # S83 v17: kept for legacy code paths but unused by new power-curve carve

# S83 v17: POWER-CURVE CARVE — lake-bowl-style depth profile.
# User feedback on v16: "Now it's a trench and it should be a bowl. Let's
# redo what's under water and make it match much more closely to how depth
# works in the lake."
# Replaces smoothstep + plateau + linear with a single soft monotonic curve.
# depth = SCALE * sdf^POWER. No plateau, no hard cap.
# Examples with SCALE=2.0, POWER=0.7:
#   SDF=0.5 (5-block channel edge):   depth = 1.2
#   SDF=2.5 (5-block channel center): depth = 4.0
#   SDF=10 (medium river center):     depth = 10.0
#   SDF=30 (wide river center):       depth = 20.6
# POWER < 1 is sublinear (soft start, doesn't blow up at wide rivers).
# Geomorphically: real rivers follow D ~ W^0.6-0.8 (Hack's law-ish).
_DEPTH_POWER_SCALE = 2.0
_DEPTH_POWER_EXPONENT = 0.7
# S84: tanh saturation toward a maximum depth. Soft cap — preserves the
# natural bowl shape (depth monotonically increases from edge to center)
# while preventing wide rivers from carving absurd 30-50 block troughs.
# User feedback: "the river carver melts too intensely- all the way down
# to a y of 1! Make rivers a more realistic depth (matches coastal shelf
# ~Y 54 = 9 blocks below sea). Don't unnaturally flatten out like a pool."
# Formula: depth = MAX * tanh(SCALE * sdf^EXP / MAX)
# tanh approaches MAX asymptotically; no hard cap = no flat plateaus.
_DEPTH_MAX_BLOCKS = 10.0
# S84: coast-distance depth taper. Real river beds ramp to natural sea
# floor depth over ~50-100 blocks approaching the ocean (delta/sediment
# effect + continental shelf bathymetry). Modulate carve_depth by
# coast_factor = tanh(coast_edt_blocks / _COAST_TAPER_BLOCKS).
# At the ocean shoreline: factor=0 → depth=0 → bed = natural sea floor.
# Far inland (~200 blocks): factor≈1 → depth = full natural carve.
# Smooth transition over ~60 blocks gives realistic delta look.
_COAST_TAPER_BLOCKS = 60.0
_RIVER_BED_GAUSS_SIGMA_8K = 4.0  # gaussian sigma for global bed smooth at 8k
                                  # (≈ σ=24 at 50k, matches per-tile σ=16×3-pass)
# S83 v11: WorldEdit //brush smooth ×5 equivalent applied to the trough
# surface AFTER the initial σ=4 bed smooth. User feedback on v8c3:
# "weird, big chunks, as if there were rectangular prisms stuck into the
# walls of the river trough". Five additional weighted gaussian passes at
# σ=2 (≈12 blocks at 50k) inside the footprint round those anomalies into
# a continuous trough surface. Same architecture as the carver's per-tile
# bank smooth-brush (BANK_SIGMA=16 × BANK_PASSES=3) but global at 8k.
_RIVER_TROUGH_SMOOTH_PASSES = 5
_RIVER_TROUGH_SMOOTH_SIGMA_8K = 2.0
# S83 v11: gentle bank gaussian on a 2-cell ring OUTSIDE the footprint.
# Smooths terrain just outside the trough wall so banks flow more
# gradually into the river — eliminates 1-2 block bank anomalies without
# pulling banks toward bed elevation. Operates on the LUT(height) values
# of bank cells only; footprint cells already contain smoothed bed and
# are protected by the weighted gaussian's mask.
_BANK_RING_RADIUS_8K = 2
_BANK_RING_GAUSS_SIGMA_8K = 0.7
# S83 v11: invariant guard — widen the bed override slightly so the
# trough wall sits FURTHER from the centerline than the widest water cell.
# The water_y_field gate uses water_zone (footprint & ~lake). If footprint
# extends past the visible water boundary, every water cell has a clean
# bed below it AND its lateral neighbor is in the trough as well. This is
# a small extra-cell bias (1.0 of SDF SOFTNESS); raise if visible bank
# poking-through returns.
_TROUGH_EXPAND_BIAS_8K = 1.0  # in 8k pixels (~6 50k blocks)

# S83 v15: LINEAR PAST PLATEAU (replaces v14 bowl bonus).
# User direction: "get rid of the max depth so it can be any depth".
# After the smoothstep ramps depth from 0 to MAX over SDF [0, INWARD_BIAS],
# depth continues to grow LINEARLY with SDF past the plateau — UNCAPPED.
# Final depth at center scales with channel width:
#   30-block river (half-width=15): depth = 6 + (15-4)*0.5 = 11.5
#   60-block river (half-width=30): depth = 6 + (30-4)*0.5 = 19
#   100-block river (half-width=50): depth = 6 + (50-4)*0.5 = 29
# The MELT GAUSSIAN downstream tames these into a smooth "melted" profile.
_DEPTH_LINEAR_RATE = 0.5      # blocks of extra depth per block of SDF past plateau

# S83 v15: MELT GAUSSIAN — VoxelSniper /b e smooth style.
# After all bed processing (smoothing + geomorph + bowl-linear-past-plateau
# + bank-asym), apply a moderate gaussian to the full bed cache to give
# the river a "melted" look. Bbox-optimized to avoid OOM.
_MELT_GAUSSIAN_SIGMA_8K = 4.0  # ~24 blocks at 50k; "decently sizeable, not massive"

# ─── S83 v12: REAL-RIVER GEOMORPHOLOGY ────────────────────────────────
# User direction (after v11): make the bed asymmetric like a real
# meandering river. At each meander bend the OUTSIDE (cut bank) is
# deeper and the INSIDE (point bar) is shallower. Also overlay
# bedform texture + riffle-pool sequence along the flow direction.
#
# Implementation uses the spline-polygon vector representation (already
# computed at 50k subpixel precision in `_build_spline_outline_50k`):
#   - tangent t̂ at each polygon point via finite difference along the
#     polygon (smoothed with σ=4 1D gaussian)
#   - signed curvature κ via dθ/ds (smoothed σ=8)
#   - cumulative arclength s
# For each 8k cell in the footprint: nearest polygon point via cKDTree
# gives κ, s, and a signed perpendicular distance to the outline.
# Thalweg bias = -κ × perp_dist × scale → outside-of-bend deeper.
# Bedform + riffle-pool biases = A·sin(2π s / λ).
#
# All amplitudes in MC BLOCKS, wavelengths in MC BLOCKS at 50k.
# Tunables (per user "1=Yes, 2=Yes, 4=Yes; 3=No substrate"):
_THALWEG_AMP_BLOCKS = 2.5         # max +/- depth offset at typical perp dist (S83 v13 keep)
_THALWEG_SCALE = 25.0             # kappa x perp_dist multiplier (8k units)
# Sign of -κ×perp×SCALE depends on: (a) find_contours CCW traversal
# convention (interior on left), (b) image y-flip, (c) np.unwrap
# direction. Derivation suggests +1 produces correct cut-bank-deeper
# behavior; if v12 render shows INVERTED asymmetry (shallow on outside
# of bend), flip to -1 and re-render — no other code changes needed.
_THALWEG_SIGN = +1.0
# Same potential sign issue for bank asymmetry. Tracks _THALWEG_SIGN —
# if asymmetric banks come out backwards (steep ramp on inside of bend,
# flat cliff on outside), flip together with _THALWEG_SIGN.
_BANK_ASYM_SIGN = +1.0
_BEDFORM_AMP_BLOCKS = 1.2         # texture-scale ripple amplitude (S83 v13: 0.8 -> 1.2)
_BEDFORM_WAVELEN_BLOCKS = 30.0    # ~bedform scale (sand dunes / ripples)
_RIFFLE_AMP_BLOCKS = 2.5          # pool-riffle alternation amplitude (S83 v13: 1.5 -> 2.5)
_RIFFLE_WAVELEN_BLOCKS = 250.0    # ~5-7× channel width = riffle-pool
# Asymmetric bank ring smoothing: cut bank stays steep (σ small),
# point bar smooths gently into a ramp (σ larger).
_BANK_ASYM_SIGMA_POINTBAR_8K = 2.5  # S83 v13: 1.6 -> 2.5; dramatic point-bar ramp
_BANK_ASYM_SIGMA_CUTBANK_8K = 0.0   # S83 v13: 0.3 -> 0.0; no smoothing = max cliff sharpness
_BANK_ASYM_RING_RADIUS_8K = 6        # S83 v13: 3 -> 6; ramp extends further inland
# S83 v12 memory note: bank asymmetry runs two full-8192² gaussian_filter
# calls + two mask arrays = ~1GB of additional peak memory on top of
# _ensure_caches's existing ~2GB of 8k float32 intermediates. Gated off
# by default so v12 can ship the (smaller) geomorph pass without OOM.
# Set True only when running with one worker AND verifying memory.
_S83_V12_BANK_ASYM_ENABLED = True   # S83 v13: enabled (bbox-optimized, OOM-safe)
# S81 v8: spline-fit river outline. Vector representation of the painted
# river boundary at 50k subpixel precision. Per-tile SDF computation uses
# these points directly (via cKDTree distance) instead of computing SDF
# from the binary 8k pixel mask — eliminates 8k-lattice quantization.
_river_spline_pts_50k_cache: np.ndarray | None = None  # (N, 2) float, [x, y] in 50k coords
_river_spline_kdtree_cache = None  # scipy.spatial.cKDTree on _river_spline_pts_50k_cache
_river_spline_polygons_50k_cache: list | None = None  # list of (Mi, 2) arrays for inside-test
# S83 v8: GLOBAL river bed elevation at 8k (analogous to rebuild_sand_dunes.py's
# 1:8 global precompute). Per-tile sampling via bilinear → no per-tile gaussian
# boundary artifacts. Replaces the per-tile bank smooth-brush gaussian which was
# the visible bed-elevation seam source.
_river_bed_8k_cache: np.ndarray | None = None  # float32 (8192, 8192) MC Y values
# S84: coast-distance depth modulation factor, computed once in _ensure_caches.
# Values in [0, 1]: 0 at ocean shoreline (no carve), →1 far inland (full carve).
_coast_factor_8k_cache: np.ndarray | None = None
# S83 v9: GLOBAL river water_y at 8k via skeleton-graph monotonic-descent walk.
# Same architecture as bed cache but for water-surface elevation. Walks the
# painted skeleton source→sink (Kahn's topo sort), running-min of LUT(height),
# then EDT-propagates per-skel water_y to all painted cells. Carver reads this
# and overrides water_y_field at painted-river cells. Eliminates the 2-block
# water_y step at tile boundaries (caused by per-tile gravity-walk asymmetry
# with the lake) without touching lakes or BLEND cascade in run_pipeline.
_river_water_y_8k_cache: np.ndarray | None = None  # float32 (8192, 8192) MC Y values
_cache_path: Path | None = None


def _build_river_edges(paint_mask_8k: np.ndarray):
    """Skeletonize a painted-river binary mask + extract per-cell paint
    half-width via EDT, so wide brush strokes carve as wide channels.

    Returns (edges, width_8k, skel_8k):
      edges    : list of (y1, x1, y2, x2) in 8192 coords.
      width_8k : float32 (8192, 8192) — distance from each painted cell
                 to the nearest non-painted cell. At skeleton cells this
                 IS the local half-width of the user's stroke at 8192 px.
      skel_8k  : bool (8192, 8192) — 1-pixel skeleton of the paint.

    S80 v33: previously emitted only edges; carver applied a fixed
    width=3, collapsing 30-px-wide brush strokes to ~7-block channels.
    EDT-derived per-cell width restores the painter's intent.

    S81: now takes a binary mask (not the raw painted PNG) so the same
    helper can re-run after orphan-endpoint extension stamps bridge
    cells into the paint.
    """
    from core.region_overlay_smoothing import clean_painted_river_mask
    from scipy.ndimage import distance_transform_edt
    if not paint_mask_8k.any():
        empty = np.zeros(paint_mask_8k.shape, dtype=np.float32)
        return [], empty, np.zeros_like(paint_mask_8k)
    skel_8k = clean_painted_river_mask(paint_mask_8k, opening_radius=2)
    width_8k = distance_transform_edt(paint_mask_8k).astype(np.float32)
    ys, xs = np.where(skel_8k)
    skel_set = set(zip(ys.tolist(), xs.tolist()))
    edges: list[tuple[int, int, int, int]] = []
    for dy, dx in ((0, 1), (1, -1), (1, 0), (1, 1)):
        for y, x in zip(ys.tolist(), xs.tolist()):
            ny, nx = y + dy, x + dx
            if (ny, nx) in skel_set:
                edges.append((y, x, ny, nx))
    return edges, width_8k, skel_8k


def _load_height_8k(masks_dir: Path) -> np.ndarray | None:
    """Read masks/height.tif and downsample to 8192×8192 for paint-
    resolution terrain queries. Returns uint16 raw values (HIGH = HIGH
    terrain per Vandir spline). None if file missing / unreadable.

    Uses nearest-neighbour resampling — we only need ORDINAL terrain
    values (which neighbour is higher) for flow-direction inference
    along the skeleton, not pixel-perfect heights. nearest is ~5×
    faster than average on a 50k uint16 source.
    """
    height_tif = masks_dir / "height.tif"
    if not height_tif.exists():
        return None
    try:
        import rasterio
        from rasterio.enums import Resampling
        with rasterio.open(height_tif) as src:
            return src.read(1, out_shape=(_REGION_PX, _REGION_PX),
                            resampling=Resampling.nearest)
    except Exception:
        return None


def _compute_flow_accumulation(shape: tuple[int, int],
                                out_edges: dict,
                                in_degree: dict,
                                ) -> np.ndarray:
    """For each painted skeleton cell, compute upstream flow accumulation
    in 8k cell counts (each cell counts itself + all upstream cells).

    Returns uint32 array of ``shape``. Zero outside the skeleton; ≥1
    inside. Topological-sort (Kahn's). Cells stuck in a cycle (DEM
    noise on a plateau) still emit their seed value of 1.
    """
    flow = np.zeros(shape, dtype=np.uint32)
    if not in_degree:
        return flow
    from collections import deque
    flow_count: dict[tuple[int, int], int] = {p: 1 for p in in_degree}
    indeg_remaining = dict(in_degree)
    queue: deque = deque(p for p, d in in_degree.items() if d == 0)
    while queue:
        p = queue.popleft()
        for d in out_edges.get(p, ()):
            flow_count[d] += flow_count[p]
            indeg_remaining[d] -= 1
            if indeg_remaining[d] == 0:
                queue.append(d)
    cap = int(np.iinfo(flow.dtype).max)
    for (y, x), c in flow_count.items():
        flow[y, x] = c if c < cap else cap
    return flow


def _build_flow_graph(edges: list[tuple[int, int, int, int]],
                       height_8k: np.ndarray,
                       ) -> tuple[dict, dict]:
    """Build (out_edges, in_degree) directed-graph dicts from a
    skeleton-edge list, using terrain elevation to set direction.
    Higher raw height = upstream (Vandir polarity).

    Edges are deduplicated by the canonical (min_p, max_p) endpoint
    pair before direction assignment.
    """
    out_edges: dict[tuple[int, int], list[tuple[int, int]]] = {}
    in_degree: dict[tuple[int, int], int] = {}
    seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for y1, x1, y2, x2 in edges:
        p1 = (y1, x1)
        p2 = (y2, x2)
        canon = (p1, p2) if p1 < p2 else (p2, p1)
        if canon in seen:
            continue
        seen.add(canon)
        h1 = int(height_8k[y1, x1])
        h2 = int(height_8k[y2, x2])
        if h1 > h2:
            up, dn = p1, p2
        elif h2 > h1:
            up, dn = p2, p1
        else:
            up, dn = canon  # deterministic on tied terrain
        out_edges.setdefault(up, []).append(dn)
        out_edges.setdefault(dn, out_edges.get(dn, []))
        in_degree[dn] = in_degree.get(dn, 0) + 1
        in_degree.setdefault(up, in_degree.get(up, 0))
    return out_edges, in_degree




def _build_spline_outline_50k(paint_mask_8k: np.ndarray,
                                smoothness_factor: float = 1.0,
                                # ── Meander tuning (WP river_script1.7-inspired) ──
                                periodic: bool = True,
                                periodic_amp_blocks: float = 6.0,
                                periodic_wavelength_blocks: float = 140.0,
                                phase_distortion_amp_blocks: float = 350.0,
                                phase_distortion_wavelength_blocks: float = 800.0,
                                micro_amp_blocks: float = 1.0,
                                micro_wavelength_blocks: float = 30.0,
                                meander_seed: int = 0xDEADBEEF):
    """Vectorise the painted river outline as periodic B-splines, then
    perpendicular-displace each spline sample to mimic natural meander.

    Returns (spline_points_50k, polygons_50k):
      - spline_points_50k: (N, 2) float32 array of densely-sampled spline
        points in 50k coords [x, y]. Used by per-tile SDF via cKDTree.
      - polygons_50k: list of (Mi, 2) arrays, one per closed contour.

    Each painted contour at 8k is fitted to a periodic B-spline (closed
    loop). Smoothness `s = smoothness_factor × len(contour)`:
      low (0.1×): exact paint shape (preserves staircase)
      mid (1.0×): WorldEdit smooth-brush feel
      high (3-5×): noticeably rounder corners (current default)

    ── Meander modes ──

    `periodic=True` (DEFAULT, WP river_script1.7 style):
      Sin wave along the bank with phase modulated by very-low-freq
      simplex noise + small high-freq simplex jitter. Looks like real
      river meander: a characteristic wavelength (~140 blocks) of
      lazy curves whose PHASE drifts smoothly across the world due to
      the 800-block-wavelength simplex modulator. NOT a perfect
      sinusoid — phase wobble breaks regularity, micro octave adds
      bank irregularity.
        formula:  disp = sin((s + simplex_macro × phase_amp) × 2π / λ)
                       × periodic_amp + simplex_micro × micro_amp

    `periodic=False` (raw two-octave noise):
      Just two-octave Simplex sampled at world coords with the
      `periodic_amp/wavelength` and `micro_amp/wavelength` knobs as
      the LOW and HIGH octaves. Simpler, less river-like, more
      "wobbly random".

    Both modes guarantee adjacent spline points see correlated
    displacement so a narrow channel meanders rather than just
    getting fatter/thinner. Set any `*_amp_blocks=0` to disable that
    component.
    """
    from skimage.measure import find_contours
    from scipy.interpolate import splprep, splev
    contours_8k = find_contours(paint_mask_8k.astype(np.float32), 0.5)
    if not contours_8k:
        return (np.zeros((0, 2), dtype=np.float32), [])
    scale = _WORLD_PX / _REGION_PX  # 8k → 50k coords (~6.1)

    # Build per-octave noise generators if any meander is requested.
    use_meander = (periodic_amp_blocks > 0.0 or micro_amp_blocks > 0.0)
    gen_phase = None
    gen_micro = None
    if use_meander:
        try:
            import opensimplex
            gen_phase = opensimplex.OpenSimplex(seed=int(meander_seed))
            gen_micro = opensimplex.OpenSimplex(
                seed=int(meander_seed) ^ 0xA5A5A5A5)
        except Exception:
            use_meander = False

    all_points = []
    polygons_50k = []
    for c in contours_8k:
        # c is (N, 2) of [row, col] sub-pixel coords
        if len(c) < 8:
            continue
        # Convert to (x=col, y=row) for splprep
        x_arr = c[:, 1]
        y_arr = c[:, 0]
        # Detect closed loop (first == last)
        closed = bool(np.allclose(c[0], c[-1]))
        try:
            tck, _u = splprep(
                [x_arr, y_arr],
                s=max(1.0, smoothness_factor * len(c)),
                per=closed,
            )
        except Exception:
            continue
        # Sample densely — ~10× the contour length
        n_samples = max(64, int(len(c) * 10))
        u_fine = np.linspace(0.0, 1.0, n_samples)
        sx, sy = splev(u_fine, tck)
        sx = np.asarray(sx, dtype=np.float64)
        sy = np.asarray(sy, dtype=np.float64)

        # ── Perpendicular meander displacement ──
        if use_meander:
            # Tangent via finite difference (wrap for closed loops).
            if closed:
                tx = np.roll(sx, -1) - np.roll(sx, 1)
                ty = np.roll(sy, -1) - np.roll(sy, 1)
            else:
                tx = np.gradient(sx)
                ty = np.gradient(sy)
            mag = np.hypot(tx, ty) + 1e-9
            tx /= mag; ty /= mag
            # Normal = perpendicular (rotate +90°).
            nx = -ty
            ny = tx
            # World coords (50k MC blocks) for noise sampling so
            # meander is consistent across tile boundaries.
            world_x = sx * scale
            world_y = sy * scale
            # Cumulative arclength along the spline (in MC blocks),
            # used as the periodic-sin coordinate so the meander
            # cycles ALONG the river, not in absolute world space.
            seg_dx = np.diff(world_x, prepend=world_x[0])
            seg_dy = np.diff(world_y, prepend=world_y[0])
            seg_len = np.hypot(seg_dx, seg_dy)
            arclen = np.cumsum(seg_len)

            # Sample low-freq simplex (phase modulator OR raw low octave)
            # and high-freq simplex (bank micro-jitter) at world coords.
            f_phase = 1.0 / max(phase_distortion_wavelength_blocks, 1.0)
            f_micro = 1.0 / max(micro_wavelength_blocks, 1.0)
            phase_noise = np.zeros(n_samples, dtype=np.float64)
            micro_noise = np.zeros(n_samples, dtype=np.float64)
            for i in range(n_samples):
                if (phase_distortion_amp_blocks > 0.0
                        or (not periodic and periodic_amp_blocks > 0.0)):
                    phase_noise[i] = gen_phase.noise2(
                        world_x[i] * f_phase, world_y[i] * f_phase)
                if micro_amp_blocks > 0.0:
                    micro_noise[i] = gen_micro.noise2(
                        world_x[i] * f_micro, world_y[i] * f_micro)

            if periodic:
                # WP-style: sin along arclength with phase modulated
                # by the low-freq simplex. Phase distortion in BLOCKS,
                # converted to radians via 2π/wavelength.
                phase_arg = (
                    (arclen + phase_noise * phase_distortion_amp_blocks)
                    * 2.0 * np.pi
                    / max(periodic_wavelength_blocks, 1.0)
                )
                disp_blocks = (np.sin(phase_arg) * periodic_amp_blocks
                               + micro_noise * micro_amp_blocks)
            else:
                # Raw two-octave noise (no sin modulation).
                f_low = 1.0 / max(periodic_wavelength_blocks, 1.0)
                low_noise = np.zeros(n_samples, dtype=np.float64)
                for i in range(n_samples):
                    if periodic_amp_blocks > 0.0:
                        low_noise[i] = gen_phase.noise2(
                            world_x[i] * f_low, world_y[i] * f_low)
                disp_blocks = (low_noise * periodic_amp_blocks
                               + micro_noise * micro_amp_blocks)

            # Convert displacement from MC blocks to 8k-px units, then
            # push perpendicular to the spline tangent.
            disp_8k = disp_blocks / scale
            sx = sx + nx * disp_8k
            sy = sy + ny * disp_8k

        # Convert from 8k coords to 50k coords [x, y]
        pts_50k = np.column_stack([sx * scale, sy * scale]).astype(np.float32)
        all_points.append(pts_50k)
        polygons_50k.append(pts_50k)
    if not all_points:
        return (np.zeros((0, 2), dtype=np.float32), [])
    return (np.concatenate(all_points, axis=0), polygons_50k)


def _compute_polygon_geometry(polygons_50k: list):
    """Compute per-point tangent, signed curvature, cumulative arclength
    for every spline-polygon point at 8k resolution.

    Returns (pts_yx_8k, tangent_xy_8k, kappa, arclen_8k):
      - pts_yx_8k: (N, 2) float32 array of [y, x] in 8k pixel coords
        (suitable for cKDTree)
      - tangent_xy_8k: (N, 2) float32 array of unit tangents [tx, ty]
        in 8k pixel coords; smoothed σ=4
      - kappa: (N,) float32 signed curvature in 1/(8k px); smoothed σ=8
      - arclen_8k: (N,) float32 cumulative arclength in 8k px,
        RESETS to 0 at start of each polygon component

    Returns (None, None, None, None) if polygons_50k is empty.
    """
    from scipy.ndimage import gaussian_filter1d as _gf1d
    if not polygons_50k:
        return None, None, None, None
    scale_50k_to_8k = _REGION_PX / _WORLD_PX  # ~0.164
    all_pts_yx = []
    all_tangent = []
    all_kappa = []
    all_arclen = []
    for poly_50k in polygons_50k:
        if poly_50k is None or len(poly_50k) < 4:
            continue
        # poly_50k is (M, 2) of [x, y] at 50k. Convert to 8k.
        poly_8k = np.asarray(poly_50k, dtype=np.float64) * scale_50k_to_8k
        # Spline polygons are CLOSED (start ≈ end). Use 'wrap' mode for
        # all 1D smoothing so the curve stays continuous around the loop.
        dx = np.gradient(poly_8k[:, 0])
        dy = np.gradient(poly_8k[:, 1])
        ds = np.hypot(dx, dy)
        ds_safe = np.where(ds < 1e-6, 1e-6, ds)
        tx = dx / ds_safe
        ty = dy / ds_safe
        # Smooth tangents along the curve
        tx = _gf1d(tx, sigma=4.0, mode='wrap')
        ty = _gf1d(ty, sigma=4.0, mode='wrap')
        tm = np.hypot(tx, ty)
        tm = np.where(tm < 1e-9, 1e-9, tm)
        tx /= tm
        ty /= tm
        # Cumulative arclength (RESET per polygon component)
        arclen = np.cumsum(ds)
        # Signed curvature κ = dθ/ds where θ = atan2(ty, tx).
        theta = np.arctan2(ty, tx)
        # Unwrap for finite-diff; wrap-mode unwrap not built-in so we
        # use scipy's standard unwrap which is fine for our scales.
        theta_uw = np.unwrap(theta)
        dth = np.gradient(theta_uw)
        kappa = dth / ds_safe
        kappa = _gf1d(kappa, sigma=8.0, mode='wrap')
        # Polygon stored as [x, y]; cKDTree expects [y, x] for consistency
        # with image array conventions used elsewhere.
        pts_yx = np.column_stack([poly_8k[:, 1], poly_8k[:, 0]])
        all_pts_yx.append(pts_yx.astype(np.float32))
        all_tangent.append(np.column_stack([tx, ty]).astype(np.float32))
        all_kappa.append(kappa.astype(np.float32))
        all_arclen.append(arclen.astype(np.float32))
    if not all_pts_yx:
        return None, None, None, None
    return (
        np.concatenate(all_pts_yx, axis=0),
        np.concatenate(all_tangent, axis=0),
        np.concatenate(all_kappa, axis=0),
        np.concatenate(all_arclen, axis=0),
    )


def _compute_skeleton_arclength_8k(
    skel_8k: np.ndarray,
    edges_cache: list,
):
    """Walk the painted-river skeleton in arc-order via BFS from each
    component's degree-1 endpoint. Return (pts_yx, arclen).

    - pts_yx: (N, 2) float32, skeleton cell coords [row, col] for cKDTree
    - arclen: (N,) float32, cumulative arclength in 8k px from start of
      component (RESETS to 0 per disconnected component)

    Why this exists (vs polygon-derived arclength): the polygon traces
    BOTH banks of the river, so two footprint cells at the SAME
    cross-section but on opposite banks would map to polygon arclengths
    thousands of blocks apart — producing a cross-channel chessboard
    pattern in the bedform/riffle-pool sin waves. Walking the
    SKELETON (1-pixel medial axis) gives a consistent arclength that
    is identical for both halves of the cross-section.

    Returns (None, None) if skeleton or edges are empty.
    """
    from collections import deque
    if not skel_8k.any() or not edges_cache:
        return None, None
    sk_y, sk_x = np.where(skel_8k)
    n = len(sk_y)
    if n == 0:
        return None, None
    pts_yx = np.column_stack([sk_y, sk_x]).astype(np.float32)
    # Map cell → linear index for adjacency lookup. Use a DICT not a
    # full-size 8192² int32 array — that was 256MB and triggered OOM
    # when stacked on the existing ~2GB of 8k float32 intermediates
    # in _ensure_caches. Dict with ~100k entries is ~6MB.
    cell_to_idx = {}
    for i in range(n):
        cell_to_idx[(int(sk_y[i]), int(sk_x[i]))] = i
    adj: list[list[int]] = [[] for _ in range(n)]
    for y1, x1, y2, x2 in edges_cache:
        i1 = cell_to_idx.get((int(y1), int(x1)), -1)
        i2 = cell_to_idx.get((int(y2), int(x2)), -1)
        if i1 >= 0 and i2 >= 0 and i1 != i2:
            adj[i1].append(i2)
            adj[i2].append(i1)
    arclen = np.full(n, -1.0, dtype=np.float32)
    # Phase 1: BFS from every degree-1 endpoint (covers most river
    # components — rivers usually have at least two endpoints).
    for start in range(n):
        if arclen[start] >= 0:
            continue
        if len(adj[start]) != 1:
            continue
        arclen[start] = 0.0
        q = deque([start])
        while q:
            cur = q.popleft()
            for nxt in adj[cur]:
                if arclen[nxt] < 0:
                    dy = pts_yx[nxt, 0] - pts_yx[cur, 0]
                    dx = pts_yx[nxt, 1] - pts_yx[cur, 1]
                    d = float(np.hypot(dx, dy))
                    arclen[nxt] = arclen[cur] + d
                    q.append(nxt)
    # Phase 2: any remaining cells (cycles, isolated dots): BFS from
    # arbitrary unvisited seed. Rare for river skeletons but safe.
    for start in range(n):
        if arclen[start] >= 0:
            continue
        arclen[start] = 0.0
        q = deque([start])
        while q:
            cur = q.popleft()
            for nxt in adj[cur]:
                if arclen[nxt] < 0:
                    dy = pts_yx[nxt, 0] - pts_yx[cur, 0]
                    dx = pts_yx[nxt, 1] - pts_yx[cur, 1]
                    d = float(np.hypot(dx, dy))
                    arclen[nxt] = arclen[cur] + d
                    q.append(nxt)
    return pts_yx, arclen


def _apply_river_geomorph_8k(
    bed_smooth_8k: np.ndarray,
    footprint_8k: np.ndarray,
    polygons_50k: list,
    skel_8k: np.ndarray,
    edges_cache: list,
    blocks_per_8k_px: float,
) -> np.ndarray:
    """Apply thalweg asymmetry + bedform + riffle-pool biases to the bed
    cache in-place. Returns the mutated array for chaining.

    Two parameterizations:
      - POLYGON-based (κ, perp_dist): thalweg asymmetry. The polygon's
        per-side κ flips sign between banks, but perp_dist is signed
        consistently (always positive or negative for interior cells),
        so the PRODUCT -κ×perp gives a CONSISTENT sign across the
        cross-section. Cut bank gets one sign, point bar gets the
        other → asymmetric depth offset that's geometrically correct
        despite polygon walking both banks.

      - SKELETON-based (arclen): bedform + riffle-pool. The skeleton is
        the medial axis — single point per cross-section — so two cells
        on opposite banks of the same cross-section map to the SAME
        arclen. The sin waves are coherent ALONG the channel direction
        instead of producing a cross-channel chessboard pattern (which
        is what polygon-arclen would do).
    """
    pts_yx_poly, tangent_poly, kappa_poly, _arclen_poly_unused = (
        _compute_polygon_geometry(polygons_50k))
    if pts_yx_poly is None or not footprint_8k.any():
        return bed_smooth_8k
    try:
        from scipy.spatial import cKDTree
    except Exception:
        return bed_smooth_8k
    kd_poly = cKDTree(pts_yx_poly)
    fp_ys, fp_xs = np.where(footprint_8k)
    query = np.column_stack([fp_ys, fp_xs]).astype(np.float32)
    _, idx_poly = kd_poly.query(query, k=1)

    # ── Polygon path: thalweg asymmetry ─────────────────────────────
    rel_y = fp_ys.astype(np.float32) - pts_yx_poly[idx_poly, 0]
    rel_x = fp_xs.astype(np.float32) - pts_yx_poly[idx_poly, 1]
    t_x = tangent_poly[idx_poly, 0]
    t_y = tangent_poly[idx_poly, 1]
    # Normal: rotation of tangent. Sign convention empirically picked
    # via _THALWEG_SIGN — flip if thalweg comes out on wrong side.
    n_y = t_x
    n_x = -t_y
    perp_dist_8k = rel_y * n_y + rel_x * n_x  # signed, in 8k px
    kappa_at = kappa_poly[idx_poly]
    thalweg_raw = _THALWEG_SIGN * (-kappa_at) * perp_dist_8k
    thalweg_bias = np.clip(
        thalweg_raw * np.float32(_THALWEG_SCALE),
        -_THALWEG_AMP_BLOCKS, _THALWEG_AMP_BLOCKS,
    )

    # ── Skeleton path: arclength-driven bedform + riffle-pool ───────
    pts_yx_skel, arclen_skel = _compute_skeleton_arclength_8k(
        skel_8k, edges_cache)
    if pts_yx_skel is not None and arclen_skel is not None:
        kd_skel = cKDTree(pts_yx_skel)
        _, idx_skel = kd_skel.query(query, k=1)
        arclen_at = arclen_skel[idx_skel]
    else:
        # Fall back to polygon arclength (broken cross-channel pattern,
        # but harmless for empty/missing skeleton)
        arclen_at = np.zeros(len(fp_ys), dtype=np.float32)

    # Bedform: short-wavelength ripple along flow
    lam_bed_8k = _BEDFORM_WAVELEN_BLOCKS / blocks_per_8k_px
    bedform_bias = _BEDFORM_AMP_BLOCKS * np.sin(
        2.0 * np.pi * arclen_at / lam_bed_8k)

    # Riffle-pool: long-wavelength alternation (5-7× channel width)
    lam_rp_8k = _RIFFLE_WAVELEN_BLOCKS / blocks_per_8k_px
    riffle_bias = _RIFFLE_AMP_BLOCKS * np.sin(
        2.0 * np.pi * arclen_at / lam_rp_8k)

    total = (thalweg_bias + bedform_bias + riffle_bias).astype(np.float32)
    # Subtract: positive bias = deeper = lower Y
    bed_smooth_8k[fp_ys, fp_xs] = (
        bed_smooth_8k[fp_ys, fp_xs] - total
    )
    print(f"[hydro_region_overlay] geomorph applied: "
          f"thalweg +/-{float(np.abs(thalweg_bias).max()):.2f}b, "
          f"bedform +/-{_BEDFORM_AMP_BLOCKS}b/lambda={_BEDFORM_WAVELEN_BLOCKS:.0f}, "
          f"riffle +/-{_RIFFLE_AMP_BLOCKS}b/lambda={_RIFFLE_WAVELEN_BLOCKS:.0f}")
    return bed_smooth_8k


def _apply_asymmetric_bank_smoothing_8k(
    bed_smooth_8k: np.ndarray,
    footprint_8k: np.ndarray,
    polygons_50k: list,
) -> np.ndarray:
    """Apply asymmetric bank smoothing: point-bar side (inside of bend)
    gets a wider sigma for a gentler ramp into the water; cut-bank side
    (outside of bend) gets a tight sigma so the wall stays steep.

    Operates on an N-cell ring outside the footprint at 8k. Modifies
    bed_smooth_8k in place at ring cells; returns it for chaining.

    S83 v13: BBox-optimized. Runs the gaussian only on the axis-aligned
    bounding box of bank-ring cells (with padding for kernel reach),
    instead of the full 8192x8192. Output is numerically identical to
    the full-image version because:
      - gaussian kernel has finite influence (3*sigma cells)
      - mask is 0 outside ring, so weighted blend is a no-op there
      - bbox + 3*sigma padding captures all cells that could be
        influenced
    Memory: ~10 MB peak instead of ~512 MB-1 GB.
    """
    from scipy.ndimage import (
        binary_dilation as _bd,
        gaussian_filter as _gf,
    )
    pts_yx, tangent, kappa, _arclen = _compute_polygon_geometry(polygons_50k)
    if pts_yx is None:
        return bed_smooth_8k
    try:
        from scipy.spatial import cKDTree
    except Exception:
        return bed_smooth_8k
    ring = _bd(footprint_8k,
               iterations=_BANK_ASYM_RING_RADIUS_8K) & ~footprint_8k
    if not ring.any():
        return bed_smooth_8k
    kd = cKDTree(pts_yx)
    ry, rx = np.where(ring)
    q = np.column_stack([ry, rx]).astype(np.float32)
    _, idx = kd.query(q, k=1)
    rel_y = ry.astype(np.float32) - pts_yx[idx, 0]
    rel_x = rx.astype(np.float32) - pts_yx[idx, 1]
    t_x = tangent[idx, 0]
    t_y = tangent[idx, 1]
    n_y = t_x
    n_x = -t_y
    perp_8k = rel_y * n_y + rel_x * n_x
    bend = _BANK_ASYM_SIGN * kappa[idx] * perp_8k  # >0 = cut bank, <0 = point bar

    H, W = bed_smooth_8k.shape
    # Compute bbox of ring cells with padding for kernel reach
    max_sigma = max(
        _BANK_ASYM_SIGMA_POINTBAR_8K, _BANK_ASYM_SIGMA_CUTBANK_8K)
    pad = int(np.ceil(3.0 * max(max_sigma, 0.5))) + 2
    y_min = max(0, int(ry.min()) - pad)
    y_max = min(H, int(ry.max()) + 1 + pad)
    x_min = max(0, int(rx.min()) - pad)
    x_max = min(W, int(rx.max()) + 1 + pad)
    # Crop bed and build cropped masks
    bed_crop = bed_smooth_8k[y_min:y_max, x_min:x_max].copy()
    ry_crop = ry - y_min
    rx_crop = rx - x_min
    pointbar_mask_crop = np.zeros(bed_crop.shape, dtype=np.float32)
    pointbar_mask_crop[ry_crop, rx_crop] = (bend < 0).astype(np.float32)
    cutbank_mask_crop = np.zeros(bed_crop.shape, dtype=np.float32)
    cutbank_mask_crop[ry_crop, rx_crop] = (bend >= 0).astype(np.float32)

    n_pb = int((bend < 0).sum())
    n_cb = int((bend >= 0).sum())

    # Point bar pass: gaussian smooth, blend at point-bar ring cells.
    # S83 v13: ring spans 6 cells, sigma=2.5 -> kernel reach ~8 cells.
    if pointbar_mask_crop.any() and _BANK_ASYM_SIGMA_POINTBAR_8K > 0:
        bl_crop = _gf(bed_crop, sigma=_BANK_ASYM_SIGMA_POINTBAR_8K)
        bed_crop = (pointbar_mask_crop * bl_crop
                    + (1.0 - pointbar_mask_crop) * bed_crop).astype(np.float32)

    # Cut bank pass: with sigma=0 the gaussian is identity, so the
    # blend equals bed_crop unchanged at all cells. Skip the call.
    # If a future config sets sigma > 0, restore the same crop blend.
    if cutbank_mask_crop.any() and _BANK_ASYM_SIGMA_CUTBANK_8K > 0:
        bl_crop = _gf(bed_crop, sigma=_BANK_ASYM_SIGMA_CUTBANK_8K)
        bed_crop = (cutbank_mask_crop * bl_crop
                    + (1.0 - cutbank_mask_crop) * bed_crop).astype(np.float32)

    # Stamp the crop back into the full array
    bed_smooth_8k[y_min:y_max, x_min:x_max] = bed_crop

    bbox_h = y_max - y_min
    bbox_w = x_max - x_min
    print(f"[hydro_region_overlay] bank asymmetry (bbox): "
          f"point_bar={n_pb} (sigma={_BANK_ASYM_SIGMA_POINTBAR_8K}), "
          f"cut_bank={n_cb} (sigma={_BANK_ASYM_SIGMA_CUTBANK_8K}), "
          f"bbox={bbox_h}x{bbox_w} (pad={pad})")
    return bed_smooth_8k


def _make_bed_cache_key(hr_path: Path) -> str:
    """Hash key for the disk-pickled bed cache. Invalidates when the
    paint mask, the _ensure_caches source, or any tunable changes."""
    import hashlib
    import inspect
    paint_md5 = hashlib.md5(hr_path.read_bytes()).hexdigest()
    src_md5 = hashlib.md5(inspect.getsource(_ensure_caches).encode()).hexdigest()
    tunables = (
        _DEPTH_POWER_SCALE, _DEPTH_POWER_EXPONENT, _DEPTH_MAX_BLOCKS,
        _COAST_TAPER_BLOCKS,
        _THALWEG_AMP_BLOCKS, _THALWEG_SCALE, _THALWEG_SIGN,
        _BEDFORM_AMP_BLOCKS, _BEDFORM_WAVELEN_BLOCKS,
        _RIFFLE_AMP_BLOCKS, _RIFFLE_WAVELEN_BLOCKS,
        _BANK_ASYM_SIGMA_POINTBAR_8K, _BANK_ASYM_SIGMA_CUTBANK_8K,
        _BANK_ASYM_RING_RADIUS_8K, _BANK_ASYM_SIGN,
        _MELT_GAUSSIAN_SIGMA_8K, _RIVER_BED_GAUSS_SIGMA_8K,
        _RIVER_TROUGH_SMOOTH_PASSES, _RIVER_TROUGH_SMOOTH_SIGMA_8K,
        _CARVE_MAX_DEPTH, _CARVE_SOFTNESS, _CARVE_INWARD_BIAS,
        _S83_V12_BANK_ASYM_ENABLED,
    )
    tun_md5 = hashlib.md5(repr(tunables).encode()).hexdigest()
    return f"{paint_md5}_{src_md5}_{tun_md5}"


class _BedCacheRefusal(RuntimeError):
    """Raised instead of rebuilding the bed cache while v17 exists.

    S93b: the v17 pickle is load-bearing and IRREPRODUCIBLE — the current
    bed builder produces a shallower bed ((62,61) depth 4-5 -> 1; the
    (27,34) estuary vanishes). Any silent fallback to a rebuild regresses
    every river in the world, so refusal must be FATAL for the tile, not
    converted into `return False` by a catch-all."""


def _load_bed_cache_from_disk(hr_path: Path) -> bool:
    """Try to load the bed-cache pickle from disk. Returns True on success
    (and populates module-level cache globals); False if missing, key
    mismatch, or load error.

    S83 v18: this is the key optimization that allows multi-worker
    parallelism on memory-constrained boxes. Each worker process loads
    the pre-computed bed cache from disk in ~5-10 sec with ~1.5 GB peak
    memory (vs ~4 GB peak for fresh compute). Net effect: --threads 8
    fits in 16 GB instead of OOMing at --threads 5.
    """
    global _river_edges_cache, _river_width_8k_cache
    global _paint_smooth_8k_cache, _paint_eroded_8k_cache
    global _lake_mask_cache, _flow_accum_8k_cache, _cache_path, _hw_inset_8k_cache
    global _river_spline_pts_50k_cache, _river_spline_kdtree_cache
    global _river_spline_polygons_50k_cache
    global _river_bed_8k_cache, _river_water_y_8k_cache
    global _coast_factor_8k_cache

    import os as _os
    import pickle as _pickle

    if _os.environ.get("VANDIR_NO_BED_CACHE"):
        return False

    bed_cache_path = hr_path.parent / "_bed_cache_v19.pkl"
    _v17_path = hr_path.parent / "_bed_cache_v17.pkl"

    def _migrate_v17_to_v19() -> None:
        # ── S93 MIGRATION: adopt the v17 cache in place of a rebuild ──
        # The v17 pickle encodes an OLDER bed-build's output that current
        # code does NOT reproduce (rebuilds yield a shallower bed: (62,61)
        # stream depth 4-5 -> 1, the (27,34) estuary drops below the
        # river-tag threshold entirely). The cache key hashes paint+config
        # only, so this code drift was never caught. Until the bed builder
        # is reconciled (own session), the v17 contents are LOAD-BEARING:
        # migrate them verbatim (plus the new S93 field) rather than
        # rebuilding. Boxes self-heal the same way (snapshot ships v17).
        _tmp19 = None
        try:
            with open(_v17_path, 'rb') as _f17:
                _d17 = _pickle.load(_f17)
            _d17['hw_inset_8k'] = _d17.get('hw_inset_8k')  # None if absent
            _d17['key'] = _make_bed_cache_key(hr_path)
            # Per-worker tmp name: parallel workers race this migration.
            # A shared tmp made the open()/os.replace() collide
            # (PermissionError) and the losers fell back to a rebuild
            # that then SAVED a regressed bed over the verbatim copy
            # (S93b freeze gate, diag_s93f/render.log).
            _tmp19 = bed_cache_path.with_suffix(f'.pkl.tmp{_os.getpid()}')
            with open(_tmp19, 'wb') as _f19:
                _pickle.dump(_d17, _f19,
                             protocol=_pickle.HIGHEST_PROTOCOL)
            _os.replace(_tmp19, bed_cache_path)
            print("[hydro_region_overlay] bed cache MIGRATED v17 -> "
                  "v19 (verbatim contents; rebuild would regress the "
                  "bed — see S93 handoff)")
        except Exception as _mig_exc:  # noqa: BLE001
            if _tmp19 is not None:
                try:
                    _os.remove(_tmp19)
                except OSError:
                    pass
            # On Windows os.replace also fails while another worker holds
            # the destination open — the winner's v19 is fine; wait for
            # it instead of rebuilding.
            import time as _time
            for _ in range(120):
                if bed_cache_path.exists():
                    print("[hydro_region_overlay] v17->v19 migration "
                          "lost race — another worker installed v19")
                    return
                _time.sleep(5)
            raise _BedCacheRefusal(
                f"bed cache v17->v19 migration failed "
                f"({type(_mig_exc).__name__}: {_mig_exc}) and no other "
                f"worker installed v19 — REFUSING to rebuild while v17 "
                f"exists (a rebuild silently regresses every river; see "
                f"memory/S93_wip_handoff.md)") from _mig_exc

    if not bed_cache_path.exists():
        if _v17_path.exists():
            _migrate_v17_to_v19()
        else:
            return False
    try:
        expected_key = _make_bed_cache_key(hr_path)
    except Exception as exc:
        print(f"[hydro_region_overlay] bed cache key build failed: {exc}")
        return False
    try:
        with open(bed_cache_path, 'rb') as f:
            data = _pickle.load(f)
        if data.get('key') != expected_key:
            if _v17_path.exists():
                # Stale v19 (paint/tunable/source drift — note the key
                # hashes _ensure_caches SOURCE, so any code edit there
                # flips every v19 stale). Re-migrate from v17 under the
                # new key — NEVER rebuild while v17 exists.
                print("[hydro_region_overlay] bed cache STALE key — "
                      "re-migrating v17 -> v19 (rebuild would regress)")
                try:
                    _os.remove(bed_cache_path)
                except OSError:
                    pass
                _migrate_v17_to_v19()
                with open(bed_cache_path, 'rb') as f:
                    data = _pickle.load(f)
                if data.get('key') != expected_key:
                    raise _BedCacheRefusal(
                        "bed cache key still stale after v17 "
                        "re-migration — refusing to rebuild while v17 "
                        "exists; re-run the render")
            else:
                print(f"[hydro_region_overlay] bed cache MISMATCH "
                      f"(masks/_bed_cache_v19.pkl is stale) — rebuilding")
                return False
        _river_bed_8k_cache = data['river_bed_8k']
        _paint_smooth_8k_cache = data['paint_smooth_8k']
        _paint_eroded_8k_cache = _paint_smooth_8k_cache
        _lake_mask_cache = data['lake_mask']
        _flow_accum_8k_cache = data['flow_accum_8k']
        _river_edges_cache = data['river_edges']
        _river_width_8k_cache = data['river_width_8k']
        _river_spline_pts_50k_cache = data['spline_pts']
        _river_spline_polygons_50k_cache = data['spline_polygons']
        _river_water_y_8k_cache = data['river_water_y_8k']
        # S84: coast factor; may be missing in pre-S84 caches (will be None).
        # Bed cache key includes _COAST_TAPER_BLOCKS so an old cache without
        # this field would be rejected by key mismatch above, but defensive-
        # load with .get() in case of partial pickles.
        _coast_factor_8k_cache = data.get('coast_factor_8k')
        _hw_inset_8k_cache = data.get('hw_inset_8k')  # S93
        # cKDTree is not picklable cleanly; rebuild from points (~5 sec)
        if (_river_spline_pts_50k_cache is not None
                and _river_spline_pts_50k_cache.shape[0] > 0):
            from scipy.spatial import cKDTree
            _river_spline_kdtree_cache = cKDTree(_river_spline_pts_50k_cache)
        else:
            _river_spline_kdtree_cache = None
        _cache_path = hr_path
        print(f"[hydro_region_overlay] bed cache HIT "
              f"({bed_cache_path.name}, "
              f"bed shape {_river_bed_8k_cache.shape})")
        return True
    except _BedCacheRefusal:
        raise  # refusal-to-rebuild must stay fatal, never become a rebuild
    except Exception as exc:
        print(f"[hydro_region_overlay] bed cache load failed: "
              f"{type(exc).__name__}: {exc}")
        return False


def _save_bed_cache_to_disk(hr_path: Path) -> None:
    """Save the bed cache to disk for subsequent workers to load."""
    import os as _os
    import pickle as _pickle

    if _os.environ.get("VANDIR_NO_BED_CACHE"):
        return

    bed_cache_path = hr_path.parent / "_bed_cache_v19.pkl"
    try:
        key = _make_bed_cache_key(hr_path)
    except Exception as exc:
        print(f"[hydro_region_overlay] bed cache key build failed "
              f"(skipping save): {exc}")
        return

    try:
        data = {
            'key': key,
            'river_bed_8k': _river_bed_8k_cache,
            'paint_smooth_8k': _paint_smooth_8k_cache,
            'lake_mask': _lake_mask_cache,
            'flow_accum_8k': _flow_accum_8k_cache,
            'river_edges': _river_edges_cache,
            'river_width_8k': _river_width_8k_cache,
            'spline_pts': _river_spline_pts_50k_cache,
            'spline_polygons': _river_spline_polygons_50k_cache,
            'river_water_y_8k': _river_water_y_8k_cache,
            'coast_factor_8k': _coast_factor_8k_cache,  # S84
            'hw_inset_8k': _hw_inset_8k_cache,          # S93 (uint8 blocks)
        }
        tmp_path = bed_cache_path.with_suffix('.pkl.tmp')
        with open(tmp_path, 'wb') as f:
            _pickle.dump(data, f, protocol=_pickle.HIGHEST_PROTOCOL)
        _os.replace(tmp_path, bed_cache_path)
        sz_mb = bed_cache_path.stat().st_size / (1024 * 1024)
        print(f"[hydro_region_overlay] bed cache SAVED "
              f"({bed_cache_path.name}, {sz_mb:.0f} MB)")
    except Exception as exc:
        print(f"[hydro_region_overlay] bed cache save failed: "
              f"{type(exc).__name__}: {exc}")


def _ensure_caches(hr_path: Path) -> None:
    global _river_edges_cache, _river_width_8k_cache
    global _paint_smooth_8k_cache, _paint_eroded_8k_cache
    global _lake_mask_cache, _flow_accum_8k_cache, _cache_path, _hw_inset_8k_cache
    global _river_spline_pts_50k_cache, _river_spline_kdtree_cache
    global _river_spline_polygons_50k_cache
    global _river_bed_8k_cache, _river_water_y_8k_cache
    global _coast_factor_8k_cache
    if _cache_path == hr_path:
        return
    # S83 v18: try loading from disk pickle first. Skip the ~3-4 GB peak
    # memory build if a valid cache exists from a previous process or
    # warm_cache run.
    if _load_bed_cache_from_disk(hr_path):
        return
    from PIL import Image as _PILImage
    hr_arr = np.asarray(_PILImage.open(hr_path).convert("L"), dtype=np.uint8)
    if hr_arr.shape != (_REGION_PX, _REGION_PX):
        _river_edges_cache = []
        _river_width_8k_cache = np.zeros((_REGION_PX, _REGION_PX),
                                          dtype=np.float32)
        _paint_smooth_8k_cache = np.zeros((_REGION_PX, _REGION_PX),
                                           dtype=np.float32)
        _paint_eroded_8k_cache = np.zeros((_REGION_PX, _REGION_PX),
                                           dtype=np.float32)
        _lake_mask_cache = np.zeros((_REGION_PX, _REGION_PX), dtype=bool)
        _flow_accum_8k_cache = np.zeros((_REGION_PX, _REGION_PX),
                                         dtype=np.uint32)
        _river_spline_pts_50k_cache = np.zeros((0, 2), dtype=np.float32)
        _river_spline_kdtree_cache = None
        _river_spline_polygons_50k_cache = []
        _hw_inset_8k_cache = None  # S93
    else:
        masks_dir = hr_path.parent
        # ── 1. Skeleton + edges + EDT width from the painted mask ──
        # S81 v8.4 Issue 3 fix: where painted river overlaps painted lake,
        # the RIVER carve wins. Painted lakes drawn around / underneath a
        # painted river were previously preserved at lake water level,
        # exposing a 1-block higher water surface adjacent to the river
        # at the junction. Subtracting paint_mask from lake_paint here
        # means river-painted cells are NOT lake cells — the carver
        # carves through them and applies river water_y, leaving lake
        # water only where the user painted lake-only.
        paint_mask = hr_arr == 2
        lake_paint = (hr_arr == 1) & ~paint_mask
        edges, width_8k, skel_8k = _build_river_edges(paint_mask)
        _river_edges_cache = edges
        _river_width_8k_cache = width_8k

        # ── 2. Flow accumulation along the painted skeleton ──
        # Direction inference uses height.tif at 8k. Each cell's
        # upstream-cell count drives Hack's-law additive widening
        # in apply_hydro_region_overlay.
        height_8k = _load_height_8k(masks_dir)
        if edges and height_8k is not None:
            graph_out, graph_in = _build_flow_graph(edges, height_8k)
            _flow_accum_8k_cache = _compute_flow_accumulation(
                skel_8k.shape, graph_out, graph_in)
        else:
            _flow_accum_8k_cache = np.zeros(skel_8k.shape, dtype=np.uint32)

        # (S93 headwater INSET field is built inside the bed-build section
        # below — it must scale by the S84 coast factor, which only exists
        # there. Compounding inset × coast-shallowing erased near-coast
        # tributaries entirely on the first gate: (19,76) lost all 10.6k
        # river cells.)
        _hw_inset_8k_cache = None

        # ── 5. GLOBAL signed distance field at 8k ──
        # Cache the SDF (positive inside paint, negative outside) with a
        # mild gaussian smoothing of the integer-pixel distances. The
        # per-tile rasterizer then bilinear-samples this SDF to 50k,
        # applies an additional 50k gaussian, and converts to a sigmoid-
        # shaped continuous carve-depth field. The CARVER applies that
        # depth to the terrain — water emerges from terrain intersection
        # like lakes do, with NO BINARY threshold anywhere → smooth
        # boundary at any zoom.
        from scipy.ndimage import (
            distance_transform_edt as _edt_dist,
            gaussian_filter as _gf_sdf,
        )
        if paint_mask.any():
            inside_dist = _edt_dist(paint_mask).astype(np.float32)
            outside_dist = _edt_dist(~paint_mask).astype(np.float32)
            # Positive inside paint (max ≈ paint half-width in 8k px),
            # negative outside (magnitude = distance to paint).
            sdf = inside_dist - outside_dist
            # Light gaussian smoothing of the SDF — softens the integer-
            # pixel-distance lattice into a continuous-ish field. Heavy
            # smoothing here erases thin strokes, so we keep this gentle
            # and rely on the per-tile 50k smoothing for the bulk of
            # the staircase removal.
            sdf = _gf_sdf(sdf, sigma=1.0)
            _paint_smooth_8k_cache = sdf
            _paint_eroded_8k_cache = sdf
            # ── 5b. Spline-fit the paint outline (S81 v8) ──
            # Vectorize the painted boundary as periodic B-splines, then
            # build a cKDTree of densely-sampled points for per-tile
            # SDF queries. Eliminates the 8k pixel-quantisation
            # fingerprint that the EDT-based SDF inherited.
            #
            # S82 ITEM #2: disk-persisted spline cache.
            #   The cKDTree + spline points + polygons cost ~10-15 min
            #   to rebuild per Python process for our paint complexity.
            #   For a full-world 9409-tile render with 2-4 workers and
            #   ProcessPoolExecutor (= fresh process per worker, never
            #   re-used), that's hundreds of redundant rebuilds.
            #
            #   Cache the three derived structures to
            #   ``masks/_spline_cache.pkl`` and reload on subsequent
            #   process boots when the paint mask, the algorithm
            #   source, and the meander params all match.
            #
            #   Cache key = md5(hydro_region.png bytes)
            #             + md5(_build_spline_outline_50k source)
            #             + md5(repr(explicit params dict)).
            #   Any change to any input invalidates the cache.
            #
            #   Atomic write via .tmp + os.replace so a partial pickle
            #   from a killed process cannot corrupt the cache file
            #   for other workers reading it.
            #
            #   Set env var ``VANDIR_NO_SPLINE_CACHE=1`` to force a
            #   rebuild + skip-save (debug runs / param-tuning).
            import hashlib as _hashlib
            import inspect as _inspect
            import os as _os
            import pickle as _pickle
            _SPLINE_PARAMS = {
                'smoothness_factor': 3.0,
                'periodic': True,
                'periodic_amp_blocks': 6.0,
                'periodic_wavelength_blocks': 140.0,
                'phase_distortion_amp_blocks': 350.0,
                'phase_distortion_wavelength_blocks': 800.0,
                'micro_amp_blocks': 1.0,
                'micro_wavelength_blocks': 30.0,
                'meander_seed': 0xDEADBEEF,
            }
            try:
                _paint_hash = _hashlib.md5(hr_path.read_bytes()).hexdigest()
                _code_hash = _hashlib.md5(
                    _inspect.getsource(_build_spline_outline_50k).encode()
                ).hexdigest()
                _params_hash = _hashlib.md5(
                    repr(sorted(_SPLINE_PARAMS.items())).encode()
                ).hexdigest()
                _spline_cache_key = f"{_paint_hash}_{_code_hash}_{_params_hash}"
            except Exception as _key_exc:
                _spline_cache_key = None
                print(f"[hydro_region_overlay] spline cache key build failed: "
                      f"{type(_key_exc).__name__}: {_key_exc}")

            _spline_cache_disabled = bool(
                _os.environ.get("VANDIR_NO_SPLINE_CACHE", "")
            )
            _spline_cache_path = masks_dir / "_spline_cache.pkl"
            _loaded_from_disk = False
            if (
                not _spline_cache_disabled
                and _spline_cache_key is not None
                and _spline_cache_path.exists()
            ):
                try:
                    with open(_spline_cache_path, 'rb') as _f:
                        _data = _pickle.load(_f)
                    if _data.get('key') == _spline_cache_key:
                        _river_spline_pts_50k_cache = _data['pts']
                        _river_spline_kdtree_cache = _data['kdtree']
                        _river_spline_polygons_50k_cache = _data['polygons']
                        _loaded_from_disk = True
                        print(f"[hydro_region_overlay] spline cache HIT "
                              f"({_spline_cache_path.name}, "
                              f"{_river_spline_pts_50k_cache.shape[0]} pts)")
                except Exception as _load_exc:
                    print(f"[hydro_region_overlay] spline cache load failed "
                          f"(rebuilding): {type(_load_exc).__name__}: "
                          f"{_load_exc}")

            if not _loaded_from_disk:
                try:
                    # smoothness_factor: 1.0 = WorldEdit-smooth-brush feel,
                    # 3.0 = noticeably rounder corners (washes out small
                    # contour jaggies), 5.0+ = aggressive (may over-round
                    # sharp meander bends). Other meander kwargs use the
                    # _build_spline_outline_50k defaults — keep
                    # _SPLINE_PARAMS above in sync if those change.
                    pts_50k, polygons = _build_spline_outline_50k(
                        paint_mask, smoothness_factor=3.0)
                    _river_spline_pts_50k_cache = pts_50k
                    _river_spline_polygons_50k_cache = polygons
                    if pts_50k.shape[0] > 0:
                        from scipy.spatial import cKDTree
                        _river_spline_kdtree_cache = cKDTree(pts_50k)
                    else:
                        _river_spline_kdtree_cache = None
                    # Write the cache atomically so a crashed process
                    # cannot leave a half-written pickle behind. Skip
                    # save when explicitly disabled or when key build
                    # failed (we'd rebuild anyway).
                    if (
                        not _spline_cache_disabled
                        and _spline_cache_key is not None
                    ):
                        try:
                            _tmp_path = _spline_cache_path.with_suffix(
                                '.pkl.tmp'
                            )
                            with open(_tmp_path, 'wb') as _f:
                                _pickle.dump({
                                    'key': _spline_cache_key,
                                    'pts': _river_spline_pts_50k_cache,
                                    'kdtree': _river_spline_kdtree_cache,
                                    'polygons': _river_spline_polygons_50k_cache,
                                }, _f, protocol=_pickle.HIGHEST_PROTOCOL)
                            _os.replace(_tmp_path, _spline_cache_path)
                            print(f"[hydro_region_overlay] spline cache SAVED "
                                  f"({_spline_cache_path.name}, "
                                  f"{pts_50k.shape[0]} pts)")
                        except Exception as _save_exc:
                            print(f"[hydro_region_overlay] spline cache save "
                                  f"failed: {type(_save_exc).__name__}: "
                                  f"{_save_exc}")
                except Exception as _spline_exc:
                    # Robust failure: fall through to old SDF only (no spline).
                    print(f"[hydro_region_overlay] spline-fit skipped: "
                          f"{type(_spline_exc).__name__}: {_spline_exc}")
                    _river_spline_pts_50k_cache = np.zeros((0, 2), dtype=np.float32)
                    _river_spline_kdtree_cache = None
                    _river_spline_polygons_50k_cache = []
        else:
            # Empty paint: large negative SDF everywhere (sigmoid → 0).
            _paint_smooth_8k_cache = np.full(
                paint_mask.shape, -1e6, dtype=np.float32)
            _paint_eroded_8k_cache = np.full(
                paint_mask.shape, -1e6, dtype=np.float32)
            _river_spline_pts_50k_cache = np.zeros((0, 2), dtype=np.float32)
            _river_spline_kdtree_cache = None
            _river_spline_polygons_50k_cache = []
        _lake_mask_cache = lake_paint

        # ═══════════════════════════════════════════════════════════════
        # S83 v8: GLOBAL RIVER BED PRECOMPUTE (sand-dunes architecture)
        # ═══════════════════════════════════════════════════════════════
        # Compute the smoothed river bed elevation globally at 8k once
        # here. Per-tile reads sample via bilinear → no per-tile gaussian
        # boundary artifacts → eliminates the bed-elevation seam at tile
        # boundaries by construction.
        #
        # bed_8k = LUT(height_8k) - carve_depth_8k, smoothed by a weighted
        # gaussian inside the painted footprint (matches the legacy
        # per-tile bank smooth-brush which used σ=16 × 3 passes at 50k;
        # σ=4 at 8k is equivalent in physical extent ≈ 25 blocks).
        #
        # Note _paint_smooth_8k_cache here is the SDF in PIXEL units (not
        # blocks). The smoothstep uses block-scale, so convert via the
        # 8k→50k pixel-ratio = 50000/8192 ≈ 6.1 blocks per 8k pixel.
        _river_bed_8k_cache = None
        if paint_mask.any() and _paint_smooth_8k_cache is not None:
            try:
                height_8k_for_bed = _load_height_8k(masks_dir)
                if height_8k_for_bed is not None:
                    _BLOCKS_PER_8K_PX = _WORLD_PX / _REGION_PX  # ≈ 6.1
                    _sdf_blocks_8k = (
                        _paint_smooth_8k_cache.astype(np.float32)
                        * np.float32(_BLOCKS_PER_8K_PX)
                    )

                    # S84: COAST DISTANCE EDT — modulate carve depth so painted
                    # rivers shallow as they approach the ocean. Real river
                    # mouths are shallow due to sediment deposition + shelf
                    # bathymetry. coast_factor: 0 at ocean shoreline → ~1 far
                    # inland (asymptotic via tanh). Multiplies into carve depth.
                    # Computed once here, cached in pickle, sampled at 50k in
                    # _rasterize_river_edges_tile.
                    from scipy.ndimage import distance_transform_edt as _edt_coast
                    _SEA_LEVEL_RAW = 17050  # matches LUT breakpoint for Y 63
                    _ocean_mask_8k = height_8k_for_bed < _SEA_LEVEL_RAW
                    # EDT measures inland distance; ocean cells have EDT=0.
                    _coast_edt_pixels_8k = _edt_coast(~_ocean_mask_8k).astype(np.float32)
                    _coast_edt_blocks_8k = (
                        _coast_edt_pixels_8k * np.float32(_BLOCKS_PER_8K_PX)
                    )
                    _coast_factor_8k_cache = np.tanh(
                        _coast_edt_blocks_8k / np.float32(_COAST_TAPER_BLOCKS)
                    ).astype(np.float32)

                    # S83 v17: POWER-CURVE CARVE (replaces smoothstep + plateau
                    # + linear). Soft monotonic curve, S84 adds tanh
                    # saturation toward _DEPTH_MAX_BLOCKS (no flat plateaus,
                    # asymptotic approach — preserves bowl shape).
                    # carve_depth = MAX * tanh(SCALE * max(0, sdf)^POWER / MAX)
                    # × coast_factor (0 at ocean → 1 inland).
                    # ── S93 headwater INSET field (uint8 blocks at 8k) ──
                    # inset = (1 - t) × paint half-width × coast_factor,
                    # t = clip((accum/_HW_A_REF)^_HW_EXP, _HW_T_MIN, 1).
                    # COAST-SCALED: the S84 factor already shallows the
                    # carve near the ocean; an unscaled inset COMPOUNDED
                    # with it and pushed near-coast tributaries below the
                    # river-tag threshold ((19,76) lost ALL rivers on the
                    # first gate). FLOORED: never inset below ~2.5 blocks
                    # of remaining half-width, so no channel can vanish.
                    # Width + accumulation are EDT-propagated outward so
                    # off-skeleton / spline-meandered cells inherit their
                    # reach's values. Subtracted from BOTH channel SDFs
                    # (this 8k bed + the 50k carve) so bed and footprint
                    # stay in lockstep.
                    if (_HW_INSET_ENABLED
                            and _flow_accum_8k_cache is not None
                            and _flow_accum_8k_cache.any()
                            and width_8k is not None and width_8k.any()):
                        _sk_m = _flow_accum_8k_cache > 0
                        _, _hw_idx = _edt_coast(~_sk_m, return_indices=True)
                        _fa_full = _flow_accum_8k_cache[
                            _hw_idx[0], _hw_idx[1]].astype(np.float32)
                        _t_full = np.clip(
                            (_fa_full / np.float32(_HW_A_REF))
                            ** np.float32(_HW_EXP),
                            np.float32(_HW_T_MIN), np.float32(1.0))
                        _w_m = width_8k > 0
                        _, _w_idx = _edt_coast(~_w_m, return_indices=True)
                        _w_full_blk = (width_8k[_w_idx[0], _w_idx[1]]
                                       .astype(np.float32)
                                       * np.float32(_BLOCKS_PER_8K))
                        _inset_blocks = ((np.float32(1.0) - _t_full)
                                         * _w_full_blk
                                         * _coast_factor_8k_cache)
                        # floor: keep >= ~2.5 blocks of half-width
                        _inset_blocks = np.minimum(
                            _inset_blocks,
                            np.maximum(_w_full_blk - np.float32(2.5),
                                       np.float32(0.0)))
                        _hw_inset_8k_cache = np.clip(
                            np.round(_inset_blocks), 0, 255).astype(np.uint8)
                        del (_sk_m, _hw_idx, _fa_full, _t_full, _w_m,
                             _w_idx, _w_full_blk, _inset_blocks)
                        print(f"[hydro_region_overlay] S93 headwater inset "
                              f"field built (max "
                              f"{int(_hw_inset_8k_cache.max())} blocks)")
                        _sdf_blocks_8k = (
                            _sdf_blocks_8k
                            - _hw_inset_8k_cache.astype(np.float32))
                    _sdf_pos = np.maximum(_sdf_blocks_8k, np.float32(0.0))
                    _depth_raw_8k = (
                        np.float32(_DEPTH_POWER_SCALE)
                        * np.power(_sdf_pos, np.float32(_DEPTH_POWER_EXPONENT))
                    )
                    _carve_depth_8k = (
                        np.float32(_DEPTH_MAX_BLOCKS)
                        * np.tanh(_depth_raw_8k / np.float32(_DEPTH_MAX_BLOCKS))
                        * _coast_factor_8k_cache  # S84 mouth-shallowing
                    ).astype(np.float32)
                    # LUT height → MC Y at 8k
                    _LUT_in_8k = np.array(
                        [0, 17050, 45000, 65496], dtype=np.float64)
                    _LUT_out_8k = np.array(
                        [-64, 63, 200, 448], dtype=np.float64)
                    _height_mc_8k = np.interp(
                        height_8k_for_bed.ravel().astype(np.float64),
                        _LUT_in_8k, _LUT_out_8k,
                    ).reshape(height_8k_for_bed.shape).astype(np.float32)
                    _bed_raw_8k = _height_mc_8k - _carve_depth_8k

                    # Weighted gaussian: footprint cells (carve > 0.5) get
                    # smoothed; outside-footprint cells keep raw height
                    # (so the smoothing sees bank elevation as "pad").
                    # S83 v11: WIDEN footprint by _TROUGH_EXPAND_BIAS_8K
                    # so the trough wall sits FURTHER OUT from the
                    # skeleton than any water block. The water_y gate
                    # in the carver uses `water_zone = footprint &
                    # ~lake_mask` (carver-side footprint computed from
                    # the 50k carve_depth), so we mirror that expansion
                    # here at the 8k cache level by lowering the depth
                    # threshold slightly — a bigger active region for
                    # the bed override means the wall always sits 1
                    # 8k-px (≈6 50k blocks) further out than where
                    # water can show.
                    _footprint_8k = _carve_depth_8k > 0.1
                    _w_bed_8k = _footprint_8k.astype(np.float32)
                    _cur_bed_8k = _bed_raw_8k.copy()
                    # One gaussian pass at σ=4 (≈ σ=24 at 50k = matches
                    # legacy 3×σ=16 effective extent).
                    _blurred_bed_8k = _gf_sdf(
                        _cur_bed_8k, sigma=_RIVER_BED_GAUSS_SIGMA_8K)
                    _bed_smooth_8k = (
                        _w_bed_8k * _blurred_bed_8k
                        + (1.0 - _w_bed_8k) * _cur_bed_8k
                    )

                    # ─── S83 v11: WORLDEDIT //BRUSH SMOOTH ×5 ───
                    # User feedback on v8c3: "weird, big chunks, as if
                    # there were rectangular prisms stuck into the walls
                    # of the river trough. We should just, imagine, take
                    # the worldedit //brush smooth like 5 times passed
                    # over the river trough surface."
                    #
                    # Apply 5 additional weighted gaussian passes at a
                    # smaller σ (=2 ≈ 12 blocks at 50k) restricted to
                    # the trough footprint. Each pass: cur = w*gauss(cur)
                    # + (1-w)*cur where w = footprint. The outside-
                    # footprint cells of _cur_bed_8k still carry raw
                    # LUT(height) so the gaussian sees bank elevation
                    # as the boundary condition — banks tug bed UP at
                    # the wall, smoothing away the "rectangular prism"
                    # protrusions while preserving overall depth in the
                    # interior. Same architecture as the carver's
                    # per-tile bank smooth-brush.
                    for _smooth_pass in range(_RIVER_TROUGH_SMOOTH_PASSES):
                        _bl = _gf_sdf(
                            _bed_smooth_8k,
                            sigma=_RIVER_TROUGH_SMOOTH_SIGMA_8K)
                        _bed_smooth_8k = (
                            _w_bed_8k * _bl
                            + (1.0 - _w_bed_8k) * _bed_smooth_8k
                        )

                    # ─── S83 v12: REAL-RIVER GEOMORPHOLOGY ───
                    # Apply thalweg asymmetry + bedform + riffle-pool
                    # biases BEFORE bank smoothing so the asymmetry is
                    # embedded in the trough surface. Bank smoothing
                    # follows and is ASYMMETRIC: point-bar side (inside
                    # of bend) smooths into a gentle ramp; cut-bank
                    # side (outside of bend) stays sharp.
                    try:
                        _bed_smooth_8k = _apply_river_geomorph_8k(
                            _bed_smooth_8k,
                            _footprint_8k,
                            _river_spline_polygons_50k_cache,
                            skel_8k,
                            _river_edges_cache,
                            _BLOCKS_PER_8K_PX,
                        )
                    except Exception as _gm_exc:
                        print(f"[hydro_region_overlay] geomorph skipped: "
                              f"{type(_gm_exc).__name__}: {_gm_exc}")

                    # S83 v15: bowl bonus removed. Depth now scales linearly
                    # with SDF past plateau (applied directly in carve_depth_8k
                    # above), giving naturally varying floor without an
                    # explicit per-cell bonus subtraction.
                    try:
                        if _S83_V12_BANK_ASYM_ENABLED:
                            _bed_smooth_8k = _apply_asymmetric_bank_smoothing_8k(
                                _bed_smooth_8k,
                                _footprint_8k,
                                _river_spline_polygons_50k_cache,
                            )
                        else:
                            raise RuntimeError(
                                "bank asymmetry gated off — falling back to "
                                "v11 uniform bank ring")
                    except Exception as _ba_exc:
                        # Fall back to legacy uniform bank ring on failure
                        print(f"[hydro_region_overlay] asym bank skipped, "
                              f"using legacy ring: {type(_ba_exc).__name__}"
                              f": {_ba_exc}")
                        try:
                            from scipy.ndimage import (
                                binary_dilation as _bd_ring,
                            )
                            _ring_dilated_8k = _bd_ring(
                                _footprint_8k,
                                iterations=_BANK_RING_RADIUS_8K)
                            _bank_ring_8k = (
                                _ring_dilated_8k & ~_footprint_8k
                            )
                            if _bank_ring_8k.any():
                                _w_bank_ring = _bank_ring_8k.astype(np.float32)
                                _bl_bank = _gf_sdf(
                                    _bed_smooth_8k,
                                    sigma=_BANK_RING_GAUSS_SIGMA_8K)
                                _bed_smooth_8k = (
                                    _w_bank_ring * _bl_bank
                                    + (1.0 - _w_bank_ring) * _bed_smooth_8k
                                )
                        except Exception:
                            pass

                    # ─── S83 v15: MELT GAUSSIAN (VoxelSniper /b e smooth) ───
                    # User direction: "add a final pass gaussian before you
                    # set the surface escape wall at farthest bounds. the
                    # rivers should look melted, as if with /b e smooth on
                    # voxelsniper."
                    # Applied unmasked to the entire bed cache to round out
                    # the linear-past-plateau depths into a continuous
                    # "melted" profile. Bbox-optimized (footprint + pad)
                    # so it doesn't allocate a full 8192x8192 intermediate.
                    try:
                        if _footprint_8k.any() and _MELT_GAUSSIAN_SIGMA_8K > 0:
                            _pad_melt = int(np.ceil(
                                3.0 * _MELT_GAUSSIAN_SIGMA_8K)) + 2
                            _fp_y, _fp_x = np.where(_footprint_8k)
                            _yMin = max(0, int(_fp_y.min()) - _pad_melt)
                            _yMax = min(_REGION_PX,
                                        int(_fp_y.max()) + 1 + _pad_melt)
                            _xMin = max(0, int(_fp_x.min()) - _pad_melt)
                            _xMax = min(_REGION_PX,
                                        int(_fp_x.max()) + 1 + _pad_melt)
                            _crop_melt = _bed_smooth_8k[
                                _yMin:_yMax, _xMin:_xMax]
                            _smoothed_melt = _gf_sdf(
                                _crop_melt, sigma=_MELT_GAUSSIAN_SIGMA_8K)
                            _bed_smooth_8k[
                                _yMin:_yMax, _xMin:_xMax
                            ] = _smoothed_melt.astype(np.float32)
                            _bbox_h = _yMax - _yMin
                            _bbox_w = _xMax - _xMin
                            print(f"[hydro_region_overlay] melt gaussian: "
                                  f"sigma={_MELT_GAUSSIAN_SIGMA_8K} at 8k "
                                  f"(~{_MELT_GAUSSIAN_SIGMA_8K * 6.1:.0f}b "
                                  f"at 50k), bbox={_bbox_h}x{_bbox_w} "
                                  f"(pad={_pad_melt})")
                    except Exception as _melt_exc:
                        print(f"[hydro_region_overlay] melt gaussian "
                              f"skipped: {type(_melt_exc).__name__}: "
                              f"{_melt_exc}")

                    # Legacy bank ring block kept for the trailing print stat
                    from scipy.ndimage import (
                        binary_dilation as _bd_ring,
                    )
                    _ring_dilated_8k = _bd_ring(
                        _footprint_8k, iterations=1)
                    _bank_ring_8k = _ring_dilated_8k & ~_footprint_8k
                    _bed_out_8k = _bed_smooth_8k

                    _river_bed_8k_cache = _bed_out_8k.astype(np.float32)
                    print(f"[hydro_region_overlay] global river bed "
                          f"precomputed at 8k (max_depth="
                          f"{_CARVE_MAX_DEPTH:.0f}, sigma="
                          f"{_RIVER_BED_GAUSS_SIGMA_8K:.0f}, "
                          f"footprint={int(_footprint_8k.sum())} "
                          f"bank_ring={int(_bank_ring_8k.sum())} cells)")
            except Exception as _bed_exc:  # noqa: BLE001
                print(f"[hydro_region_overlay] global bed precompute "
                      f"skipped: {type(_bed_exc).__name__}: {_bed_exc}")
                _river_bed_8k_cache = None

        # ═══════════════════════════════════════════════════════════════
        # S83 v9: GLOBAL RIVER WATER_Y PRECOMPUTE (skeleton-graph walk)
        # ═══════════════════════════════════════════════════════════════
        # Walk the painted skeleton source→sink via the flow-graph already
        # built for `_compute_flow_accumulation`. For each cell, track the
        # running minimum of LUT(height) seen from source down to this
        # cell — this is the monotonic water-surface elevation. After the
        # walk, EDT-propagate per-skel water_y to all painted-river
        # footprint cells at 8k so cross-section is uniform per Voronoi.
        # Then per-tile bilinear-sampling gives globally-consistent
        # water_y → eliminates the 2-block water step at tile boundaries
        # that the per-tile gravity walk produced.
        #
        # Lakes are NOT touched (paint_mask is id=2 only; lake_paint is
        # id=1). Lake water levels in run_pipeline.py and the BLEND_DIST
        # cascade still operate as before, just on globally-consistent
        # river water_y values.
        _river_water_y_8k_cache = None
        # S83 v8c: v9b water_y skeleton walk DISABLED — user reverted to
        # v8b behavior (per-tile gravity walk water_y, with its 2-block
        # boundary seam). The v9b skeleton walk distributed the gradient
        # along the river but introduced "land seam under water" artifacts
        # that the user found worse than the original water seam.
        _S83_V9_WATER_Y_ENABLED = False
        if (_S83_V9_WATER_Y_ENABLED
                and paint_mask.any() and edges and height_8k is not None
                and skel_8k.any()):
            try:
                # Rebuild flow graph (or reuse — _build_flow_graph is cheap
                # vs the rest of _ensure_caches). Already called earlier
                # if _compute_flow_accumulation ran, but we want explicit
                # in_degree/out_edges scoped here.
                _graph_out, _graph_in = _build_flow_graph(edges, height_8k)

                # MC-Y LUT once for vectorized later use
                _LUT_in_w = np.array(
                    [0, 17050, 45000, 65496], dtype=np.float64)
                _LUT_out_w = np.array(
                    [-64, 63, 200, 448], dtype=np.float64)

                def _height_mc_at(p):
                    return float(np.interp(
                        float(height_8k[p[0], p[1]]),
                        _LUT_in_w, _LUT_out_w,
                    ))

                # Kahn's topo sort: process in_degree=0 cells first.
                # running_min[p] = min(LUT(height) at p, min over upstream
                # predecessors of their running_min).
                from collections import deque as _deque_w
                _in_remain = dict(_graph_in)
                _running_min: dict[tuple[int, int], float] = {}
                _q = _deque_w(
                    p for p, d in _graph_in.items() if d == 0
                )
                # Initialize source water_y = LUT(height) at source.
                for _src in _q:
                    _running_min[_src] = _height_mc_at(_src)

                while _q:
                    _cell = _q.popleft()
                    _curr = _running_min.get(
                        _cell, _height_mc_at(_cell))
                    for _ds in _graph_out.get(_cell, ()):
                        _ds_h = _height_mc_at(_ds)
                        _new_min = min(_curr, _ds_h)
                        if _ds in _running_min:
                            _running_min[_ds] = min(
                                _running_min[_ds], _new_min)
                        else:
                            _running_min[_ds] = _new_min
                        _in_remain[_ds] -= 1
                        if _in_remain[_ds] == 0:
                            _q.append(_ds)

                # Build skel_water_y_8k: per skeleton cell, running_min - 1
                # (water surface sits 1 below local bank top in the
                # canonical formula).
                _skel_water_y_8k = np.full(
                    skel_8k.shape, -999.0, dtype=np.float32)
                for (_r, _c), _m in _running_min.items():
                    _skel_water_y_8k[_r, _c] = float(_m) - 1.0

                # Propagate from skeleton to all painted cells at 8k via
                # cKDTree (NOT distance_transform_edt with return_indices,
                # which allocates a (2, 8192, 8192) int32 array = 512 MiB
                # and can OOM workers running in parallel). cKDTree on
                # ~35k skeleton points uses ~MB and per-query is O(log N).
                _has_water = _skel_water_y_8k > -500.0
                _n_skel = int(_has_water.sum())
                _n_paint = int(paint_mask.sum())
                if _has_water.any() and _n_paint > 0:
                    _skel_rows, _skel_cols = np.where(_has_water)
                    _skel_pts_kd = np.column_stack(
                        [_skel_rows, _skel_cols]).astype(np.float32)
                    _skel_wy_vals = _skel_water_y_8k[_skel_rows, _skel_cols]
                    from scipy.spatial import cKDTree as _cKDTree_w
                    _kd_w = _cKDTree_w(_skel_pts_kd)
                    # Query for painted cells only (sparse subset, not all 67M).
                    _p_rows, _p_cols = np.where(paint_mask)
                    _query_pts = np.column_stack(
                        [_p_rows, _p_cols]).astype(np.float32)
                    _, _idx_w = _kd_w.query(_query_pts, k=1)
                    # Build sparse result array
                    _water_y_propagated = np.full(
                        paint_mask.shape, -999.0, dtype=np.float32)
                    _water_y_propagated[_p_rows, _p_cols] = (
                        _skel_wy_vals[_idx_w])
                    _river_water_y_8k_cache = _water_y_propagated
                    print(f"[hydro_region_overlay] global river water_y "
                          f"precomputed at 8k ({_n_skel} skel cells, "
                          f"{_n_paint} painted cells, via cKDTree)")
            except Exception as _wy_exc:  # noqa: BLE001
                print(f"[hydro_region_overlay] global water_y precompute "
                      f"skipped: {type(_wy_exc).__name__}: {_wy_exc}")
                _river_water_y_8k_cache = None
    _cache_path = hr_path
    # S83 v18: persist the bed cache to disk so subsequent workers
    # (this process or other processes on the same box) can load in
    # ~5 sec at ~1.5 GB peak memory instead of rebuilding at ~4 GB peak.
    try:
        _save_bed_cache_to_disk(hr_path)
    except Exception:
        pass


_dist_src_8k_sparse = None  # (rr, cc, dist_blocks) | False after a failed build


def _ensure_dist_from_source(masks_dir=None):
    """S93e: per-skeleton-cell GEODESIC distance (in blocks) from the
    channel's SOURCE TIP, BFS over the painted 8k skeleton pixels.

    Drives the carver's headwater width taper. Tips are degree-1
    endpoints of the flow>0 pixel set with near-zero accumulation
    (mouth endpoints carry high flow and are excluded). Pixel-BFS is
    deliberately used instead of the painted flow graph — the graph
    breaks at stroke joints (accumulation resets mid-channel), which
    is why flow alone can't drive the taper.
    """
    global _dist_src_8k_sparse
    if _dist_src_8k_sparse is not None:
        return _dist_src_8k_sparse or None
    try:
        fl = _flow_accum_8k_cache
        if fl is None or not fl.any():
            _dist_src_8k_sparse = False
            return None
        from collections import deque
        from scipy.ndimage import convolve as _cv_ds
        sk = fl > 0
        _k8 = np.ones((3, 3), np.uint8)
        _k8[1, 1] = 0
        deg = _cv_ds(sk.astype(np.uint8), _k8, mode="constant")
        tips = sk & (deg == 1) & (fl <= 5)
        # LAKE-OUTLET BONUS: a zero-flow endpoint that touches a lake is
        # an OUTLET, not a spring — the lake integrates its whole basin,
        # so the channel should START wide. Seed those tips at 300 blocks
        # (-> target ~8 half-width) instead of 0. (Lake INFLOW ends carry
        # accumulated flow > 5 and were never tips.) Lakes = painted-
        # region lakes UNION Gaea lakes (hydro_lake.tif decimated to 8k)
        # — the painted lake mask is EMPTY world-wide (all Vandir lakes
        # are Gaea), which silently no-op'd the first version and left
        # (62,61)'s junction stream 2 blocks wide.
        _lake_8k = None
        if _lake_mask_cache is not None and _lake_mask_cache.any():
            _lake_8k = _lake_mask_cache.copy()
        if masks_dir is not None:
            try:
                import rasterio as _rio_ds
                from rasterio.enums import Resampling as _Rs_ds
                with _rio_ds.open(str(masks_dir / "hydro_lake.tif")) as _lds:
                    _gl = _lds.read(
                        1, out_shape=(_REGION_PX, _REGION_PX),
                        resampling=_Rs_ds.nearest) > 0
                _lake_8k = _gl if _lake_8k is None else (_lake_8k | _gl)
            except Exception as _gl_exc:  # noqa: BLE001
                print(f"[hydro_region_overlay] gaea-lake load for outlet "
                      f"bonus failed: {type(_gl_exc).__name__}: {_gl_exc}")
        _lake_near = None
        if _lake_8k is not None and _lake_8k.any():
            from scipy.ndimage import binary_dilation as _bd_ds
            _lake_near = _bd_ds(_lake_8k, iterations=3)
        _LAKE_SRC_DIST = 300.0
        rr, cc = np.where(sk)
        index = {(int(r), int(c)): i for i, (r, c) in enumerate(zip(rr, cc))}
        dist = np.full(len(rr), -1.0, dtype=np.float32)
        q = deque()
        for r, c in zip(*np.where(tips)):
            _seed = 0.0
            if _lake_near is not None and _lake_near[r, c]:
                _seed = _LAKE_SRC_DIST
            dist[index[(int(r), int(c))]] = _seed
            q.append((int(r), int(c)))
        _step = np.float32(float(_WORLD_PX) / float(_REGION_PX))  # ~6.1 blk/px
        while q:
            r, c = q.popleft()
            d0 = dist[index[(r, c)]]
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    j = index.get((r + dr, c + dc))
                    if j is not None and dist[j] < 0:
                        dist[j] = d0 + _step
                        q.append((r + dr, c + dc))
        # S93e v2: densify with midpoints of 8-connected skeleton pairs
        # (halves the ~6.1-block point spacing → EDT scalloping ±1.5) and
        # carry the half-width (width_8k EDT value) per point so the
        # carver's taper is PURE per-cell math on globally-sampled fields
        # (the seam law: never derive geometry per-tile).
        hwv = (_river_width_8k_cache[rr, cc].astype(np.float32)
               * (float(_WORLD_PX) / float(_REGION_PX))
               if _river_width_8k_cache is not None
               else np.zeros(len(rr), np.float32))
        prr = [rr.astype(np.float32)]
        pcc = [cc.astype(np.float32)]
        pvv = [dist]
        phw = [hwv]
        for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
            j = np.array([index.get((int(r) + dr, int(c) + dc), -1)
                          for r, c in zip(rr, cc)], dtype=np.int64)
            ok = j >= 0
            if ok.any():
                i = np.where(ok)[0]
                prr.append((rr[i] + rr[j[i]]) / 2.0)
                pcc.append((cc[i] + cc[j[i]]) / 2.0)
                pvv.append((dist[i] + dist[j[i]]) / 2.0)
                phw.append((hwv[i] + hwv[j[i]]) / 2.0)
        _dist_src_8k_sparse = (
            np.concatenate(prr), np.concatenate(pcc),
            np.concatenate(pvv), np.concatenate(phw))
        print(f"[hydro_region_overlay] dist-from-source built: "
              f"{len(rr)} skel cells (+midpoints -> "
              f"{len(_dist_src_8k_sparse[0])} pts), {int(tips.sum())} tips, "
              f"max {float(dist.max()):.0f} blocks")
        return _dist_src_8k_sparse
    except Exception as _ds_exc:  # noqa: BLE001
        print(f"[hydro_region_overlay] dist-from-source build failed: "
              f"{type(_ds_exc).__name__}: {_ds_exc}")
        _dist_src_8k_sparse = False
        return None


def _windowed_map_coordinates(arr_8k, rows_f, cols_f, order, cval):
    """S93c OOM fix: map_coordinates on a tile-bbox WINDOW of an 8k field.

    scipy's order>=2 spline prefilter allocates a float64 copy of the WHOLE
    input (8192^2 = 512MB) per call, per tile — three such calls per river
    tile got worker processes OOM-killed on the 8GB local box (and waste
    ~1.5GB/worker on render boxes). The cubic prefilter is an IIR with pole
    |z| ~= 0.268, so influence decays below float32 epsilon within ~20 px;
    a 40-px margin window is numerically identical for all sampled coords
    while allocating ~(64+80)^2 instead of 8192^2.
    """
    from scipy.ndimage import map_coordinates as _mc_w
    _m = 40
    r0 = max(0, int(np.floor(rows_f.min())) - _m)
    r1 = min(arr_8k.shape[0], int(np.ceil(rows_f.max())) + _m + 1)
    c0 = max(0, int(np.floor(cols_f.min())) - _m)
    c1 = min(arr_8k.shape[1], int(np.ceil(cols_f.max())) + _m + 1)
    sub = np.ascontiguousarray(arr_8k[r0:r1, c0:c1], dtype=np.float32)
    rg, cg = np.meshgrid(rows_f - r0, cols_f - c0, indexing="ij")
    return _mc_w(sub, np.stack([rg, cg]), order=order,
                 mode="constant", cval=cval)


def _rasterize_river_edges_tile(
    col_off: int, row_off: int, tile_size: int,
):
    """Build the per-tile centerline mask + per-cell EDT width.

    Two contributions, MAX-combined:
      1. **Bresenham edges** from the global skeleton — gives smooth
         50k curves through the medial axis of painted regions.
      2. **Raw paint mask** NEAREST-cropped from the 8192 source —
         guarantees that EVERY painted cell is part of the centerline
         (and therefore the carve footprint), even non-convex / corner
         pixels that the skeleton+EDT-width might miss.

    Returns (centerline, width_radius_blocks), both (tile_size, tile_size).

    S80 v33b "Option B": adds the raw-paint floor. Previously a
    skeletonised wide brush stroke could leave 23% of painted cells
    uncovered (non-convex blobs, corner pixels). Now the painted
    region is union'd in, so paint coverage is mathematically exact.
    """
    from skimage.draw import line as draw_line
    out = np.zeros((tile_size, tile_size), dtype=bool)
    width = np.zeros((tile_size, tile_size), dtype=np.float32)
    if not _river_edges_cache and _river_width_8k_cache is None:
        return out, width
    scale = _WORLD_PX / _REGION_PX  # ~6.1 = 50k / 8192

    # ── 1. Bresenham along the global skeleton edges ──
    if _river_edges_cache:
        for y1_8k, x1_8k, y2_8k, x2_8k in _river_edges_cache:
            wy1 = y1_8k * scale; wx1 = x1_8k * scale
            wy2 = y2_8k * scale; wx2 = x2_8k * scale
            ty1 = int(wy1 - row_off); tx1 = int(wx1 - col_off)
            ty2 = int(wy2 - row_off); tx2 = int(wx2 - col_off)
            if (max(ty1, ty2) < -2 or min(ty1, ty2) > tile_size + 1 or
                    max(tx1, tx2) < -2 or min(tx1, tx2) > tile_size + 1):
                continue
            w1_8k = (_river_width_8k_cache[y1_8k, x1_8k]
                     if _river_width_8k_cache is not None else 0.0)
            w2_8k = (_river_width_8k_cache[y2_8k, x2_8k]
                     if _river_width_8k_cache is not None else 0.0)
            w1_blocks = float(w1_8k) * scale
            w2_blocks = float(w2_8k) * scale
            rr, cc = draw_line(ty1, tx1, ty2, tx2)
            valid = (rr >= 0) & (rr < tile_size) & (cc >= 0) & (cc < tile_size)
            if not valid.any():
                continue
            rr_v = rr[valid]; cc_v = cc[valid]
            out[rr_v, cc_v] = True
            n = len(rr_v)
            if n == 1:
                ws = np.array([w1_blocks])
            else:
                ts = np.linspace(0.0, 1.0, n, dtype=np.float32)
                ws = w1_blocks + (w2_blocks - w1_blocks) * ts
            prev = width[rr_v, cc_v]
            width[rr_v, cc_v] = np.maximum(prev, ws)

    # ── Spline-derived SDF → sigmoid carve-depth (S81 v8) ──
    # SDF is computed from the SPLINE-FITTED RIVER OUTLINE (continuous
    # analytical curve at 50k subpixel precision) via cKDTree distance
    # query, NOT from the binary 8k pixel mask. Eliminates the 8k-
    # lattice quantisation fingerprint that the EDT-based SDF inherited.
    #
    # Falls back to the old EDT-cache SDF if no spline (paint missing
    # or splprep failed).
    # S83 v8: use module-level constants (also used by _ensure_caches for
    # global bed precompute, so both paths agree on params).
    _SDF_SMOOTH_SIGMA_50K = 4.0  # 50k gaussian sigma for SDF smoothing (fallback path)
    paint_smooth_full_50k = None
    flow_50k = np.zeros((tile_size, tile_size), dtype=np.float32)
    carve_depth_50k = None
    paint_eroded_50k = None
    edt_blocks = None

    # Try spline-based SDF first — best quality, eliminates 8k lattice.
    use_spline = (
        _river_spline_kdtree_cache is not None
        and _river_spline_pts_50k_cache is not None
        and _river_spline_pts_50k_cache.shape[0] > 0
        and _river_spline_polygons_50k_cache
    )
    if use_spline:
        # Build (x, y) coords for every cell in this tile in 50k space
        ys = np.arange(tile_size, dtype=np.float32) + row_off
        xs = np.arange(tile_size, dtype=np.float32) + col_off
        yg, xg = np.meshgrid(ys, xs, indexing="ij")
        tile_pts = np.column_stack(
            [xg.ravel(), yg.ravel()]).astype(np.float32)
        # Distance from each tile cell to nearest spline curve point.
        dist_50k_flat, _ = _river_spline_kdtree_cache.query(tile_pts, k=1)
        # Sign via point-in-polygon: union of all spline polygons.
        # S84 PERF FIX: bbox cull each polygon against the current tile's
        # 50k bbox before doing the (expensive) inside-test on all 262144
        # tile cells. Profile (py-spy 2026-05-22 on pure-ocean (48,48))
        # showed `contains_points` consuming 68% of total wall time on
        # tiles where ZERO polygons actually intersect — the loop was
        # testing every painted spline polygon globally against every
        # cell in the tile.
        # bbox check: ~4×N_vertices min/max ops per polygon (microseconds);
        # skipped contains_points call: ~262K-vertex-O ops saved (minutes).
        # For an ocean tile this drops _rasterize_river_edges_tile from
        # ~20 min to ~negligible.
        from matplotlib.path import Path as _MplPath
        inside_mask_flat = np.zeros(tile_pts.shape[0], dtype=bool)
        _tile_min_x = float(col_off)
        _tile_max_x = float(col_off + tile_size)
        _tile_min_y = float(row_off)
        _tile_max_y = float(row_off + tile_size)
        _n_tested = 0
        _n_skipped = 0
        for poly in _river_spline_polygons_50k_cache:
            # Cheap bbox overlap check first.
            if poly is None or poly.shape[0] == 0:
                _n_skipped += 1
                continue
            _pmin_x = float(poly[:, 0].min())
            _pmax_x = float(poly[:, 0].max())
            if _pmax_x < _tile_min_x or _pmin_x > _tile_max_x:
                _n_skipped += 1
                continue
            _pmin_y = float(poly[:, 1].min())
            _pmax_y = float(poly[:, 1].max())
            if _pmax_y < _tile_min_y or _pmin_y > _tile_max_y:
                _n_skipped += 1
                continue
            _n_tested += 1
            try:
                inside_mask_flat |= _MplPath(poly).contains_points(tile_pts)
            except Exception:
                pass
        # Lightweight diagnostic (per-tile, one line) to surface savings.
        if (_n_tested + _n_skipped) > 0:
            print(
                f"[hydro_region_overlay] tile=({col_off // tile_size},"
                f"{row_off // tile_size}) spline polygons: "
                f"{_n_tested} tested, {_n_skipped} skipped via bbox cull",
                flush=True,
            )
        sdf_blocks = np.where(
            inside_mask_flat, dist_50k_flat, -dist_50k_flat
        ).reshape(tile_size, tile_size).astype(np.float32)
        # S93 headwater inset at 50k — the SAME field subtracted from the
        # 8k bed SDF, bilinear-sampled (cval 0 = no inset off-world). Keeps
        # footprint (this sdf) and bed (8k) in lockstep — the S92 v4 lesson.
        if _hw_inset_8k_cache is not None:
            from scipy.ndimage import map_coordinates as _mc_hw
            _s8 = _REGION_PX / _WORLD_PX
            _rows_hw = (np.arange(tile_size, dtype=np.float64) + row_off) * _s8
            _cols_hw = (np.arange(tile_size, dtype=np.float64) + col_off) * _s8
            _rg_hw, _cg_hw = np.meshgrid(_rows_hw, _cols_hw, indexing="ij")
            _inset_50k = _mc_hw(
                _hw_inset_8k_cache.astype(np.float32),
                np.stack([_rg_hw, _cg_hw]), order=1,
                mode="constant", cval=0.0).astype(np.float32)
            sdf_blocks = sdf_blocks - _inset_50k
            del _inset_50k, _rg_hw, _cg_hw, _rows_hw, _cols_hw
        # S83 v17 + S84: power curve with tanh saturation (matches
        # _ensure_caches formula). depth = MAX * tanh(SCALE * sdf^POWER / MAX)
        # × coast_factor (0 at ocean → 1 inland, sampled at 50k from 8k cache).
        _sdf_pos_50k = np.maximum(sdf_blocks, 0.0)
        _depth_raw_50k = (
            np.float32(_DEPTH_POWER_SCALE)
            * np.power(_sdf_pos_50k, np.float32(_DEPTH_POWER_EXPONENT))
        )
        carve_depth_50k = (
            np.float32(_DEPTH_MAX_BLOCKS)
            * np.tanh(_depth_raw_50k / np.float32(_DEPTH_MAX_BLOCKS))
        ).astype(np.float32)
        # Width sampling still uses the 8k EDT cache (cheap, OK quality)
        from scipy.ndimage import map_coordinates as _mc
        scale_to_8k = _REGION_PX / _WORLD_PX
        rows_f = (np.arange(tile_size, dtype=np.float64) + row_off) * scale_to_8k
        cols_f = (np.arange(tile_size, dtype=np.float64) + col_off) * scale_to_8k
        rg, cg = np.meshgrid(rows_f, cols_f, indexing="ij")
        coords = np.stack([rg, cg])
        # S93c: windowed — the full-field order-3 prefilter was a 512MB
        # float64 alloc per call (OOM-killed workers; see helper docstring).
        edt_at_tile_8k = _windowed_map_coordinates(
            _river_width_8k_cache, rows_f, cols_f, 3, 0.0)
        edt_blocks = edt_at_tile_8k * scale
        # S84: sample coast factor (8k cache) at 50k via bilinear interp.
        if _coast_factor_8k_cache is not None:
            _coast_factor_50k = _mc(
                _coast_factor_8k_cache, coords, order=1,
                mode="constant", cval=0.0,
            ).astype(np.float32)
            carve_depth_50k = (
                carve_depth_50k * _coast_factor_50k
            ).astype(np.float32)
        paint_eroded_50k = carve_depth_50k > 0.5
        paint_smooth_full_50k = paint_eroded_50k.copy()
    elif _paint_eroded_8k_cache is not None:
        # Fallback: old SDF-from-pixel-mask path
        from scipy.ndimage import map_coordinates as _mc
        from scipy.ndimage import gaussian_filter as _gf_50k
        scale_to_8k = _REGION_PX / _WORLD_PX
        rows_f = (np.arange(tile_size, dtype=np.float64) + row_off) * scale_to_8k
        cols_f = (np.arange(tile_size, dtype=np.float64) + col_off) * scale_to_8k
        rg, cg = np.meshgrid(rows_f, cols_f, indexing="ij")
        coords = np.stack([rg, cg])

        sdf_50k = _windowed_map_coordinates(
            _paint_eroded_8k_cache, rows_f, cols_f, 3, -1e6)
        sdf_50k = _gf_50k(sdf_50k, sigma=_SDF_SMOOTH_SIGMA_50K)
        sdf_blocks = sdf_50k * scale  # 8k pixels → MC blocks
        # S83 v17 + S84: power curve with tanh saturation (matches spline path)
        # × coast_factor (sampled at 50k from 8k cache).
        _sdf_pos_50k_fb = np.maximum(sdf_blocks, 0.0)
        _depth_raw_50k_fb = (
            np.float32(_DEPTH_POWER_SCALE)
            * np.power(_sdf_pos_50k_fb, np.float32(_DEPTH_POWER_EXPONENT))
        )
        carve_depth_50k = (
            np.float32(_DEPTH_MAX_BLOCKS)
            * np.tanh(_depth_raw_50k_fb / np.float32(_DEPTH_MAX_BLOCKS))
        ).astype(np.float32)
        # S84: sample coast factor at 50k via bilinear interp (fallback path
        # reuses the same coords as _paint_eroded_8k_cache sampling above).
        if _coast_factor_8k_cache is not None:
            _coast_factor_50k_fb = _mc(
                _coast_factor_8k_cache, coords, order=1,
                mode="constant", cval=0.0,
            ).astype(np.float32)
            carve_depth_50k = (
                carve_depth_50k * _coast_factor_50k_fb
            ).astype(np.float32)
        paint_eroded_50k = carve_depth_50k > 0.5
        paint_smooth_full_50k = paint_eroded_50k.copy()

        edt_at_tile_8k = _windowed_map_coordinates(
            _river_width_8k_cache, rows_f, cols_f, 3, 0.0)
        edt_blocks = edt_at_tile_8k * scale

    # ── Common post-processing (runs for both spline and fallback paths) ──
    if paint_eroded_50k is not None and paint_eroded_50k.any():
        out |= paint_eroded_50k
        if edt_blocks is not None:
            width = np.maximum(
                width, edt_blocks * paint_eroded_50k.astype(np.float32)
            )

    # Flow accumulation (S81): bilinear-sample upstream cell counts
    # along the painted skeleton.
    if (_flow_accum_8k_cache is not None
            and _flow_accum_8k_cache.any()
            and paint_eroded_50k is not None):
        scale_to_8k = _REGION_PX / _WORLD_PX
        rows_f = (np.arange(tile_size, dtype=np.float64) + row_off) * scale_to_8k
        cols_f = (np.arange(tile_size, dtype=np.float64) + col_off) * scale_to_8k
        # S93c: windowed (was a full-8k float32 copy + 512MB float64
        # prefilter per tile).
        flow_at_tile_8k = _windowed_map_coordinates(
            _flow_accum_8k_cache, rows_f, cols_f, 3, 0.0)
        flow_50k = np.where(paint_eroded_50k, flow_at_tile_8k, 0.0)

    return out, width, paint_smooth_full_50k, flow_50k, carve_depth_50k


def _rasterize_lake_mask_tile(
    col_off: int, row_off: int, tile_size: int,
) -> np.ndarray:
    """NEAREST-crop the 8192 lake mask (id=1) to tile resolution."""
    out = np.zeros((tile_size, tile_size), dtype=bool)
    if _lake_mask_cache is None or not _lake_mask_cache.any():
        return out
    scale = _REGION_PX / _WORLD_PX
    sx0 = max(0, int(col_off * scale))
    sy0 = max(0, int(row_off * scale))
    sx1 = min(_REGION_PX, max(sx0 + 1, int((col_off + tile_size) * scale) + 1))
    sy1 = min(_REGION_PX, max(sy0 + 1, int((row_off + tile_size) * scale) + 1))
    slab = _lake_mask_cache[sy0:sy1, sx0:sx1]
    rows_src = np.linspace(0, slab.shape[0] - 1, tile_size).astype(np.int32)
    cols_src = np.linspace(0, slab.shape[1] - 1, tile_size).astype(np.int32)
    return slab[rows_src[:, None], cols_src[None, :]]


def apply_hydro_region_overlay(
    masks: dict,
    masks_dir: Path,
    col_off: int,
    row_off: int,
    tile_size: int,
    *,
    verbose: bool = False,
) -> dict:
    """Mutate ``masks`` in place to apply hydro_region.png overrides.

    For the current tile footprint, reads any painted id=1 (lake) / id=2
    (river) pixels and forces the corresponding hydro_* masks to express
    those features.  Returns the same ``masks`` dict for chaining.
    """
    hr_path = Path(masks_dir) / "hydro_region.png"
    if not hr_path.exists():
        return masks

    try:
        _ensure_caches(hr_path)
    except Exception as e:
        if verbose:
            print(f"[hydro_region_overlay] skip — cache build failed: {e}")
        return masks

    # Rivers come from the globally-skeletonized edge set rasterized per-tile
    # as Bresenham lines — produces smooth 50k curves, not the staircased
    # block corners you'd get from a naive NEAREST upsample.
    # river_width_radius is the EDT-derived half-width per cell, in MC
    # blocks. Wide brush stroke → wide river. Thin stroke → thin river.
    river_paint, river_width_radius, paint_full_smooth, flow_50k, carve_depth_50k = (
        _rasterize_river_edges_tile(col_off, row_off, tile_size)
    )
    lake_paint = _rasterize_lake_mask_tile(col_off, row_off, tile_size)

    # ════════════════════════════════════════════════════════════════════
    # S80 v32: hydro_region.png IS THE SOLE SOURCE OF RIVERS.
    # ════════════════════════════════════════════════════════════════════
    # Previous behaviour was ADDITIVE — `np.maximum` OR'd painted rivers
    # on top of the existing WP-findPath hydro_centerline.tif, so every
    # carved tile contained BOTH the WP-generated network AND the
    # user's strokes. That made painted output indistinguishable from
    # WP output at a glance.
    #
    # New behaviour: when ANY painted river exists globally (i.e. the
    # skeleton edge cache is non-empty), the precompute hydro_centerline
    # / hydro_order / hydro_width / hydro_depth values for this tile
    # are ZEROED before the paint is applied. Only the painted edges
    # contribute to the carved river network. The on-disk TIFs are
    # untouched — this wipe is in-memory per-tile.
    #
    # If hydro_region.png is empty / absent the wipe does NOT fire,
    # falling back to the legacy WP-findPath rivers — so removing your
    # paint cleanly reverts to the precompute-driven hydrology.
    # ════════════════════════════════════════════════════════════════════
    has_global_paint = bool(_river_edges_cache)
    if has_global_paint:
        for key in ("hydro_centerline", "hydro_order",
                     "hydro_width", "hydro_depth"):
            arr = masks.get(key)
            if arr is not None:
                if not arr.flags.writeable:
                    arr = arr.copy()
                arr[:] = 0
                masks[key] = arr

    if not (river_paint.any() or lake_paint.any()):
        return masks

    # ---- Rivers: width modulation ----
    # Erosion is now done GLOBALLY at 8k in _ensure_caches, so
    # river_paint here is already the eroded shape (no tile-boundary
    # truncation). paint_full_smooth is the un-eroded smooth mask
    # passed through from the rasterizer for mouth-restore.
    if river_paint.any():
        # Mouth restore: within 80 blocks of any sink, swap the eroded
        # carve mask for the full smoothed mask so river mouths don't
        # shrink as much as their middles. This is a SHAPE concern,
        # separate from per-cell width modulation below.
        try:
            from scipy.ndimage import distance_transform_edt as _edt2
            ht = masks.get("height")
            sink_mask = lake_paint.copy() if lake_paint.any() else \
                        np.zeros_like(river_paint)
            if ht is not None:
                sink_mask = sink_mask | (ht <= (17050.0 / 65535.0))
            lk_id = masks.get("hydro_lake")
            lk_wl = masks.get("hydro_lake_wl")
            if lk_id is not None and lk_wl is not None and ht is not None:
                sink_mask = sink_mask | ((lk_id > 0) & (ht < lk_wl))

            if sink_mask.any() and paint_full_smooth is not None:
                d_to_sink = _edt2(~sink_mask).astype(np.float32)
                near_sink = d_to_sink < 80
                river_paint_modulated = (
                    river_paint | (paint_full_smooth & near_sink)
                )
            else:
                river_paint_modulated = river_paint

            if not river_paint_modulated.any():
                river_paint_modulated = river_paint
        except Exception:
            river_paint_modulated = river_paint

        # ── NORMALISATION — masks dict values are tile_streamer-normalised ──
        # tile_streamer.read_tile() divides uint8 mask values by 255 on load
        # (and uint16 by 65535) so every mask in the dict is float32 in [0,1].
        # When the carver re-quantises via _denorm_u8 it multiplies by 255 to
        # recover the original uint8 value. So when WE write into the dict
        # we MUST divide by 255 — otherwise a raw value like 1 (centerline
        # marker) would round-trip to 1*255=255 = the legacy carver's
        # ``braid_fill_mask = precomp_cl == 255`` sentinel and every painted
        # river cell gets reinterpreted as wide solid braid water.
        # _denorm_u16 (lakes) is 65535-scale, hydro_lake_wl is float passthrough.
        cl = masks.get("hydro_centerline")
        if cl is not None:
            cl = cl.copy() if not cl.flags.writeable else cl
            cl[river_paint_modulated] = np.maximum(
                cl[river_paint_modulated], 1.0 / 255.0)
            masks["hydro_centerline"] = cl
        ord_ = masks.get("hydro_order")
        if ord_ is not None:
            ord_ = ord_.copy() if not ord_.flags.writeable else ord_
            np.maximum(ord_, _RIVER_ORDER_MIN / 255.0,
                        where=river_paint_modulated, out=ord_)
            masks["hydro_order"] = ord_

        # From here on, river_paint refers to the post-erosion carve mask
        river_paint = river_paint_modulated

        # Per-cell width = EDT-derived half-width of the painted area.
        # NOT used to extend the footprint (footprint = river_full_mask).
        # USED by the carver only as the WP river_script1.7 width input
        # for U-shape depth scaling: deeper at center, shallower at
        # banks, with depth proportional to width per WP's
        # `depth = width * 0.25 + 1.5`. Slope modifier still applied
        # (flatter = wider effective river bed).
        per_cell_radius = river_width_radius.copy()
        slope_mask = masks.get("slope")
        if slope_mask is not None:
            s_norm = np.clip(slope_mask.astype(np.float32), 0.0, 1.0)
            per_cell_radius = per_cell_radius * (1.0 - 0.4 * s_norm)

        # Per-cell width in MC blocks → write into normalised float32 dict
        # by dividing by 255 (the uint8 quantisation step in _denorm_u8).
        # Clamp to 254 blocks max (== 254/255 normalised) so we never collide
        # with the carver's 255-as-braid sentinel after round-trip.
        w = masks.get("hydro_width")
        if w is not None:
            w = w.copy() if not w.flags.writeable else w
            radius_clipped = np.clip(per_cell_radius, 0, 254)
            w[river_paint] = (radius_clipped[river_paint]
                              / 255.0).astype(w.dtype)
            masks["hydro_width"] = w

        # hydro_depth carries the SIGMOID-shaped CARVE DEPTH per cell
        # (S81 v4 terrain-intersection approach). The carver reads
        # depth_u8 = _denorm_u8(hydro_depth) and uses it directly as
        # the per-cell carve depth — no internal U-shape formula. Smooth
        # depth field → smooth carved terrain → smooth water boundary
        # via terrain intersection (no binary threshold = no staircase).
        d = masks.get("hydro_depth")
        if d is not None and carve_depth_50k is not None:
            d = d.copy() if not d.flags.writeable else d
            # Set depth on EVERY cell where carve_depth has any meaningful
            # value (not just river_paint), so the carver sees the full
            # smooth gradient including the soft edges. Clip to 254 to
            # stay inside uint8 quantisation after _denorm_u8 round-trip.
            carve_clipped = np.clip(carve_depth_50k, 0, 254)
            # S81 v8: WIDE buffer (carve > 0.02 blocks ≈ sdf > -22 blocks
            # past paint). The carver applies depth subtraction on every
            # cell with non-zero hydro_depth and uses the same field as
            # the water_y_field gate, so this push the binary mask
            # boundary FAR beyond the visible water — terrain
            # intersection alone defines what's water vs land.
            has_carve = carve_clipped > 0.02
            d[has_carve] = (carve_clipped[has_carve]
                            / 255.0).astype(d.dtype)
            masks["hydro_depth"] = d

    # ---- Lakes: force lake ID + depth (normalised values, see note above) ──
    if lake_paint.any():
        lk = masks.get("hydro_lake")
        if lk is not None:
            lk = lk.copy() if not lk.flags.writeable else lk
            new_lake_mask = lake_paint & (lk == 0)
            # _denorm_u16 multiplies by 65535 so divide by 65535 here.
            lk[new_lake_mask] = _LAKE_ID_DEFAULT / 65535.0
            masks["hydro_lake"] = lk
        ld = masks.get("hydro_lkdep")
        if ld is not None:
            ld = ld.copy() if not ld.flags.writeable else ld
            np.maximum(ld, _LAKE_DEPTH_MIN / 255.0,
                        where=lake_paint, out=ld)
            masks["hydro_lkdep"] = ld

    # ---- S83 v8: Sample GLOBAL river bed at 50k tile coords ──
    # The bed_8k cache was computed once globally in _ensure_caches.
    # Sample to 50k tile bilinearly → smooth by construction, identical
    # values at tile boundaries when both tiles sample the same physical
    # coords. Write to masks dict as raw MC-Y float (carver reads it).
    if (_river_bed_8k_cache is not None
            or _river_water_y_8k_cache is not None):
        try:
            from scipy.ndimage import map_coordinates as _mc_bed
            _scale_50k_to_8k = _REGION_PX / _WORLD_PX
            _rows_f = (
                (np.arange(tile_size, dtype=np.float64) + row_off)
                * _scale_50k_to_8k
            )
            _cols_f = (
                (np.arange(tile_size, dtype=np.float64) + col_off)
                * _scale_50k_to_8k
            )
            _rg, _cg = np.meshgrid(_rows_f, _cols_f, indexing="ij")
            _coords = np.stack([_rg, _cg])
            if _river_bed_8k_cache is not None:
                _bed_50k = _mc_bed(
                    _river_bed_8k_cache, _coords, order=1,
                    mode='constant', cval=0.0,
                ).astype(np.float32)
                masks["hydro_river_bed"] = _bed_50k
            # S83 v9: also sample the global water_y cache to 50k tile.
            # Carver overrides water_y_field at painted-river cells.
            if _river_water_y_8k_cache is not None:
                _wy_50k = _mc_bed(
                    _river_water_y_8k_cache, _coords, order=1,
                    mode='constant', cval=-999.0,
                ).astype(np.float32)
                masks["hydro_river_water_y"] = _wy_50k
            # S93e v2: headwater-taper fields — GLOBAL 8k skeleton points
            # rasterized into a HALO'D 50k window, one EDT, crop to tile.
            # Both sides of any seam see the same points within the halo
            # (64 blocks >> max target half-width ~25), so distance-to-
            # centerline, distance-from-source and half-width agree across
            # tiles — the per-tile skeletonize this replaces dried the
            # first rows of (27,34) at the (27,33) border (its medial axis
            # stopped at the window cut).
            _ds = _ensure_dist_from_source(masks_dir)
            if _ds is not None:
                _dsr, _dsc, _dsv, _dsh = _ds
                _HALO_DS = 64
                _b50 = float(_WORLD_PX) / float(_REGION_PX)
                _r050 = row_off - _HALO_DS
                _c050 = col_off - _HALO_DS
                _hh = tile_size + 2 * _HALO_DS
                # points (8k px, centers) → 50k block coords → window px
                _pr = np.round((_dsr + 0.5) * _b50).astype(np.int64) - _r050
                _pc = np.round((_dsc + 0.5) * _b50).astype(np.int64) - _c050
                _in_w = ((_pr >= 0) & (_pr < _hh) & (_pc >= 0) & (_pc < _hh))
                if _in_w.any():
                    from scipy.ndimage import (
                        distance_transform_edt as _edt_ds)
                    _occ = np.zeros((_hh, _hh), dtype=bool)
                    _val = np.zeros((_hh, _hh), dtype=np.float32)
                    _hwv = np.zeros((_hh, _hh), dtype=np.float32)
                    _occ[_pr[_in_w], _pc[_in_w]] = True
                    _val[_pr[_in_w], _pc[_in_w]] = _dsv[_in_w]
                    _hwv[_pr[_in_w], _pc[_in_w]] = _dsh[_in_w]
                    _dcl_w, _idx_w = _edt_ds(~_occ, return_indices=True)
                    _crop_ds = np.s_[_HALO_DS:_HALO_DS + tile_size,
                                     _HALO_DS:_HALO_DS + tile_size]
                    masks["hydro_dcl"] = (
                        _dcl_w[_crop_ds].astype(np.float32))
                    masks["hydro_dist_src"] = (
                        _val[_idx_w[0], _idx_w[1]][_crop_ds]
                        .astype(np.float32))
                    masks["hydro_hw_cl"] = (
                        _hwv[_idx_w[0], _idx_w[1]][_crop_ds]
                        .astype(np.float32))
        except Exception as _bed_samp_exc:  # noqa: BLE001
            print(f"[hydro_region_overlay] bed/water_y sample skipped: "
                  f"{type(_bed_samp_exc).__name__}: {_bed_samp_exc}")

    if verbose:
        stats = (
            f"river(spline)={int(river_paint.sum())} "
            f"lake={int(lake_paint.sum())} "
            f"edges={len(_river_edges_cache or [])}"
        )
        print(f"[hydro_region_overlay] applied: {stats}")

    return masks

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
# S81 v8: spline-fit river outline. Vector representation of the painted
# river boundary at 50k subpixel precision. Per-tile SDF computation uses
# these points directly (via cKDTree distance) instead of computing SDF
# from the binary 8k pixel mask — eliminates 8k-lattice quantization.
_river_spline_pts_50k_cache: np.ndarray | None = None  # (N, 2) float, [x, y] in 50k coords
_river_spline_kdtree_cache = None  # scipy.spatial.cKDTree on _river_spline_pts_50k_cache
_river_spline_polygons_50k_cache: list | None = None  # list of (Mi, 2) arrays for inside-test
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


def _ensure_caches(hr_path: Path) -> None:
    global _river_edges_cache, _river_width_8k_cache
    global _paint_smooth_8k_cache, _paint_eroded_8k_cache
    global _lake_mask_cache, _flow_accum_8k_cache, _cache_path
    global _river_spline_pts_50k_cache, _river_spline_kdtree_cache
    global _river_spline_polygons_50k_cache
    if _cache_path == hr_path:
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
    else:
        masks_dir = hr_path.parent
        # ── 1. Skeleton + edges + EDT width from the painted mask ──
        paint_mask = hr_arr == 2
        lake_paint = hr_arr == 1
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
            try:
                # smoothness_factor: 1.0 = WorldEdit-smooth-brush feel, 3.0 =
                # noticeably rounder corners (washes out small contour jaggies),
                # 5.0+ = aggressive (may over-round sharp meander bends).
                pts_50k, polygons = _build_spline_outline_50k(
                    paint_mask, smoothness_factor=3.0)
                _river_spline_pts_50k_cache = pts_50k
                _river_spline_polygons_50k_cache = polygons
                if pts_50k.shape[0] > 0:
                    from scipy.spatial import cKDTree
                    _river_spline_kdtree_cache = cKDTree(pts_50k)
                else:
                    _river_spline_kdtree_cache = None
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
    _cache_path = hr_path


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
    _CARVE_MAX_DEPTH = 4.0       # blocks below water at paint center
    _CARVE_SOFTNESS = 2.5        # blocks of soft transition each side of paint edge (sharper than v8 orig 4.0)
    _SDF_SMOOTH_SIGMA_50K = 4.0  # 50k gaussian sigma for SDF smoothing (fallback path)
    # Positive bias shifts the sigmoid centre INWARD (toward paint interior),
    # which chokes the carved river below the painted footprint. 0 = water
    # zone matches paint extent, 2-3 = subtle (gets eaten by soft sigmoid tail),
    # 5+ = visibly skinnier when paired with sharper SOFTNESS.
    _CARVE_INWARD_BIAS = 5.0
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
        from matplotlib.path import Path as _MplPath
        inside_mask_flat = np.zeros(tile_pts.shape[0], dtype=bool)
        for poly in _river_spline_polygons_50k_cache:
            try:
                inside_mask_flat |= _MplPath(poly).contains_points(tile_pts)
            except Exception:
                pass
        sdf_blocks = np.where(
            inside_mask_flat, dist_50k_flat, -dist_50k_flat
        ).reshape(tile_size, tile_size).astype(np.float32)
        # Sigmoid carve depth — smooth transition, no threshold edge,
        # no 8k-lattice fingerprint. _CARVE_INWARD_BIAS shifts the half-
        # depth contour inward by N blocks → chokes the visible water by
        # N on every side without changing paint geometry.
        carve_depth_50k = (
            _CARVE_MAX_DEPTH
            / (1.0 + np.exp(
                -(sdf_blocks - _CARVE_INWARD_BIAS) / _CARVE_SOFTNESS))
        )
        paint_eroded_50k = carve_depth_50k > 0.5
        paint_smooth_full_50k = paint_eroded_50k.copy()
        # Width sampling still uses the 8k EDT cache (cheap, OK quality)
        from scipy.ndimage import map_coordinates as _mc
        scale_to_8k = _REGION_PX / _WORLD_PX
        rows_f = (np.arange(tile_size, dtype=np.float64) + row_off) * scale_to_8k
        cols_f = (np.arange(tile_size, dtype=np.float64) + col_off) * scale_to_8k
        rg, cg = np.meshgrid(rows_f, cols_f, indexing="ij")
        coords = np.stack([rg, cg])
        edt_at_tile_8k = _mc(_river_width_8k_cache, coords, order=3,
                             mode="constant", cval=0.0)
        edt_blocks = edt_at_tile_8k * scale
    elif _paint_eroded_8k_cache is not None:
        # Fallback: old SDF-from-pixel-mask path
        from scipy.ndimage import map_coordinates as _mc
        from scipy.ndimage import gaussian_filter as _gf_50k
        scale_to_8k = _REGION_PX / _WORLD_PX
        rows_f = (np.arange(tile_size, dtype=np.float64) + row_off) * scale_to_8k
        cols_f = (np.arange(tile_size, dtype=np.float64) + col_off) * scale_to_8k
        rg, cg = np.meshgrid(rows_f, cols_f, indexing="ij")
        coords = np.stack([rg, cg])

        sdf_50k = _mc(_paint_eroded_8k_cache, coords, order=3,
                      mode="constant", cval=-1e6)
        sdf_50k = _gf_50k(sdf_50k, sigma=_SDF_SMOOTH_SIGMA_50K)
        sdf_blocks = sdf_50k * scale  # 8k pixels → MC blocks
        carve_depth_50k = (
            _CARVE_MAX_DEPTH
            / (1.0 + np.exp(
                -(sdf_blocks - _CARVE_INWARD_BIAS) / _CARVE_SOFTNESS))
        )
        paint_eroded_50k = carve_depth_50k > 0.5
        paint_smooth_full_50k = paint_eroded_50k.copy()

        edt_at_tile_8k = _mc(_river_width_8k_cache, coords, order=3,
                             mode="constant", cval=0.0)
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
        from scipy.ndimage import map_coordinates as _mc_flow
        scale_to_8k = _REGION_PX / _WORLD_PX
        rows_f = (np.arange(tile_size, dtype=np.float64) + row_off) * scale_to_8k
        cols_f = (np.arange(tile_size, dtype=np.float64) + col_off) * scale_to_8k
        rg, cg = np.meshgrid(rows_f, cols_f, indexing="ij")
        coords = np.stack([rg, cg])
        flow_at_tile_8k = _mc_flow(
            _flow_accum_8k_cache.astype(np.float32),
            coords, order=3, mode="constant", cval=0.0)
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

    if verbose:
        stats = (
            f"river(spline)={int(river_paint.sum())} "
            f"lake={int(lake_paint.sum())} "
            f"edges={len(_river_edges_cache or [])}"
        )
        print(f"[hydro_region_overlay] applied: {stats}")

    return masks

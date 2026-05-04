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
_paint_smooth_8k_cache: np.ndarray | None = None    # smoothed binary
_paint_eroded_8k_cache: np.ndarray | None = None    # smoothed + eroded
_lake_mask_cache: np.ndarray | None = None
_cache_path: Path | None = None


def _build_river_edges(hr_arr: np.ndarray):
    """Skeletonize painted rivers + extract per-cell paint half-width
    via EDT, so wide brush strokes carve as wide channels.

    Returns (edges, width_8k):
      edges    : list of (y1, x1, y2, x2) in 8192 coords.
      width_8k : float32 (8192, 8192) — distance from each painted cell
                 to the nearest non-painted cell. At skeleton cells this
                 IS the local half-width of the user's stroke at 8192 px.

    S80 v33: previously emitted only edges; carver applied a fixed
    width=3, collapsing 30-px-wide brush strokes to ~7-block channels.
    EDT-derived per-cell width restores the painter's intent.
    """
    from core.region_overlay_smoothing import clean_painted_river_mask
    from scipy.ndimage import distance_transform_edt
    river_mask_8k = hr_arr == 2
    if not river_mask_8k.any():
        return [], np.zeros(hr_arr.shape, dtype=np.float32)
    skel_8k = clean_painted_river_mask(river_mask_8k, opening_radius=2)
    width_8k = distance_transform_edt(river_mask_8k).astype(np.float32)
    ys, xs = np.where(skel_8k)
    skel_set = set(zip(ys.tolist(), xs.tolist()))
    edges: list[tuple[int, int, int, int]] = []
    for dy, dx in ((0, 1), (1, -1), (1, 0), (1, 1)):
        for y, x in zip(ys.tolist(), xs.tolist()):
            ny, nx = y + dy, x + dx
            if (ny, nx) in skel_set:
                edges.append((y, x, ny, nx))
    return edges, width_8k


def _ensure_caches(hr_path: Path) -> None:
    global _river_edges_cache, _river_width_8k_cache
    global _paint_smooth_8k_cache, _paint_eroded_8k_cache
    global _lake_mask_cache, _cache_path
    if _cache_path == hr_path:
        return
    from PIL import Image as _PILImage
    hr_arr = np.asarray(_PILImage.open(hr_path).convert("L"), dtype=np.uint8)
    if hr_arr.shape != (_REGION_PX, _REGION_PX):
        _river_edges_cache = []
        _river_width_8k_cache = np.zeros((_REGION_PX, _REGION_PX),
                                          dtype=np.float32)
        _paint_smooth_8k_cache = np.zeros((_REGION_PX, _REGION_PX), dtype=bool)
        _paint_eroded_8k_cache = np.zeros((_REGION_PX, _REGION_PX), dtype=bool)
        _lake_mask_cache = np.zeros((_REGION_PX, _REGION_PX), dtype=bool)
    else:
        _river_edges_cache, _river_width_8k_cache = _build_river_edges(hr_arr)
        # ── GLOBAL shape post-processing at 8k ──
        # All smoothing + erosion happens here ONCE on the full 8192
        # paint mask. Per-tile rasterize then bilinear-samples these
        # caches, so tile boundaries never see "outside-paint = false"
        # truncation.
        from scipy.ndimage import (gaussian_filter as _gf,
                                    binary_erosion as _be8)
        paint_mask = hr_arr == 2
        if paint_mask.any():
            smooth1 = _gf(paint_mask.astype(np.float32), sigma=4.0) > 0.30
            smooth2 = _gf(smooth1.astype(np.float32), sigma=1.0) > 0.45
            _paint_smooth_8k_cache = smooth2
            # Erosion at 8k — 2 cells ≈ 12 MC blocks at 50k. Same
            # shrink amount as v34h's per-tile erosion=12, but global
            # so no tile-boundary truncation.
            _paint_eroded_8k_cache = _be8(smooth2, iterations=2)
        else:
            _paint_smooth_8k_cache = np.zeros_like(paint_mask)
            _paint_eroded_8k_cache = np.zeros_like(paint_mask)
        _lake_mask_cache = (hr_arr == 1)
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

    # ── Globally-smoothed paint floor (bilinear-sampled, v34i) ──
    # All smoothing + erosion happens ONCE at 8k in _ensure_caches.
    # Here we just bilinear-sample the cached eroded mask, plus the
    # cached full-smooth mask (so apply_hydro_region_overlay can
    # restore mouth widths via union with the full mask near sinks).
    paint_smooth_full_50k = None
    if _paint_eroded_8k_cache is not None:
        from scipy.ndimage import map_coordinates as _mc
        scale_to_8k = _REGION_PX / _WORLD_PX
        rows_f = (np.arange(tile_size, dtype=np.float64) + row_off) * scale_to_8k
        cols_f = (np.arange(tile_size, dtype=np.float64) + col_off) * scale_to_8k
        rg, cg = np.meshgrid(rows_f, cols_f, indexing="ij")
        coords = np.stack([rg, cg])

        # Eroded smoothed mask — the DEFAULT carve area
        eroded_f = _mc(_paint_eroded_8k_cache.astype(np.float32),
                       coords, order=3, mode="constant", cval=0.0)
        paint_eroded_50k = eroded_f > 0.5
        # Full smoothed mask — used by mouth-restore in apply_overlay
        full_f = _mc(_paint_smooth_8k_cache.astype(np.float32),
                     coords, order=3, mode="constant", cval=0.0)
        paint_smooth_full_50k = full_f > 0.5

        # EDT for per-cell width (sampled smoothly from 8k)
        edt_at_tile_8k = _mc(_river_width_8k_cache, coords, order=3,
                             mode="constant", cval=0.0)
        edt_blocks = edt_at_tile_8k * scale

        if paint_eroded_50k.any():
            out |= paint_eroded_50k
            width = np.maximum(
                width, edt_blocks * paint_eroded_50k.astype(np.float32)
            )

    return out, width, paint_smooth_full_50k


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
    river_paint, river_width_radius, paint_full_smooth = (
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

    # ---- Rivers: width modulation (v34i) ----
    # Erosion is now done GLOBALLY at 8k in _ensure_caches, so
    # river_paint here is already the eroded shape (no tile-boundary
    # truncation). paint_full_smooth is the un-eroded smooth mask
    # passed through from the rasterizer for mouth-restore.
    if river_paint.any():
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
                under = (lk_id > 0) & (ht < lk_wl)
                sink_mask = sink_mask | under

            if sink_mask.any() and paint_full_smooth is not None:
                d_to_sink = _edt2(~sink_mask).astype(np.float32)
                MOUTH_RANGE = 80
                near_sink = d_to_sink < MOUTH_RANGE
                river_paint_modulated = (
                    river_paint | (paint_full_smooth & near_sink)
                )
            else:
                river_paint_modulated = river_paint

            if not river_paint_modulated.any():
                river_paint_modulated = river_paint

            # ── Lake-shore bridge (v34j) ──
            # If a painted river ends inside a basin (hydro_lake>0) but
            # short of the actual underwater area (terrain<wl), there's
            # a visible dry-shore gap where the trench can't reach the
            # lake water. Bridge it: within the basin, find cells that
            # are BOTH near the painted river (within 30 blocks) AND
            # near underwater (within 30 blocks). Those cells fill the
            # shore-gap, so the trench reads continuously into the lake.
            if (lk_id is not None and lk_wl is not None and ht is not None):
                from scipy.ndimage import binary_dilation as _bd
                basin_local = (lk_id > 0)
                underwater_local = basin_local & (ht < lk_wl)
                if underwater_local.any():
                    near_river = _bd(river_paint_modulated, iterations=30)
                    near_water = _bd(underwater_local, iterations=30)
                    bridge = basin_local & near_river & near_water
                    if bridge.any():
                        river_paint_modulated = river_paint_modulated | bridge
        except Exception:
            river_paint_modulated = river_paint

        cl = masks.get("hydro_centerline")
        if cl is not None:
            cl = cl.copy() if not cl.flags.writeable else cl
            cl[river_paint_modulated] = np.maximum(
                cl[river_paint_modulated], 1)
            masks["hydro_centerline"] = cl
        ord_ = masks.get("hydro_order")
        if ord_ is not None:
            ord_ = ord_.copy() if not ord_.flags.writeable else ord_
            np.maximum(ord_, _RIVER_ORDER_MIN,
                        where=river_paint_modulated, out=ord_)
            masks["hydro_order"] = ord_

        # From here on, river_paint refers to the post-erosion carve mask
        river_paint = river_paint_modulated

        # Per-cell width: EDT-derived (v34h, no aggressive cap).
        per_cell_radius = river_width_radius.copy()

        # Slope modifier: flatter terrain → wider, steeper → narrower.
        slope_mask = masks.get("slope")
        if slope_mask is not None:
            s_norm = np.clip(slope_mask.astype(np.float32), 0.0, 1.0)
            # 1.0 at slope=0 (flat → 100% width)
            # 0.6 at slope=1 (steep → 60% width)
            slope_factor = 1.0 - 0.4 * s_norm
            per_cell_radius = per_cell_radius * slope_factor

        # ── Distance-to-sink TAPER ──
        # User intent: rivers should be NARROW in the middle of their
        # course, WIDER near mouths (lakes / ocean), and tapered toward
        # both ends.
        # 1. Globally squeeze the EDT-derived width (×0.4) — middles are
        #    much thinner than the painted blob suggests.
        # 2. Near a sink (within 60 blocks), boost up to ×2.5 — restoring
        #    full or extra width at the mouth.
        try:
            from scipy.ndimage import distance_transform_edt as _edt
            ht = masks.get("height")
            sink_mask = lake_paint.copy() if lake_paint.any() else \
                        np.zeros_like(river_paint)
            if ht is not None:
                ocean_here = ht <= (17050.0 / 65535.0)
                sink_mask = sink_mask | ocean_here
            # Underwater-lake mask (real terrain-intersection lakes
            # are also sinks, even if not painted).
            lk_id = masks.get("hydro_lake")
            lk_wl = masks.get("hydro_lake_wl")
            if lk_id is not None and lk_wl is not None and ht is not None:
                # both stored as float [0,1] from tile_streamer
                under = (lk_id > 0) & (ht < lk_wl)
                sink_mask = sink_mask | under

            if sink_mask.any():
                d_to_sink = _edt(~sink_mask).astype(np.float32)
                # Bell-shaped taper around the river path:
                # near=0 at d=0 (sink boundary), near=1 at d>=60 blocks.
                far = np.clip(d_to_sink / 60.0, 0.0, 1.0)
                # Width factor: 1.0 at sink (full / boosted), 0.4 far away.
                # Pure base: 0.4 (squeezed middles); near boost up to +1.5
                # so mouths reach ~2.5× to widen visibly into deltas.
                near = 1.0 - far
                taper_factor = 0.4 + 1.5 * (near ** 2)
                per_cell_radius = per_cell_radius * taper_factor
            else:
                # No sink in this tile → just squeeze uniformly.
                per_cell_radius = per_cell_radius * 0.45
        except Exception:
            per_cell_radius = per_cell_radius * 0.45

        # No floor, no cap (v34) — width fully driven by paint thickness.
        # 1-px creek paint produces a 1-block stream; massive paint
        # produces correspondingly wide rivers. uint8 quantisation
        # clipping below is the only constraint.

        # Write to hydro_width — uint8 quantise (clip to dtype range).
        w = masks.get("hydro_width")
        if w is not None:
            w = w.copy() if not w.flags.writeable else w
            dtype_max = np.iinfo(w.dtype).max if np.issubdtype(w.dtype, np.integer) else 255
            radius_clipped = np.clip(per_cell_radius, 0, dtype_max)
            w[river_paint] = radius_clipped[river_paint].astype(w.dtype)
            masks["hydro_width"] = w

        d = masks.get("hydro_depth")
        if d is not None:
            d = d.copy() if not d.flags.writeable else d
            # Depth scales with width: depth ≈ radius × 0.5 + 1
            depth_per_cell = (per_cell_radius * 0.5 + 1.0).astype(d.dtype)
            d[river_paint] = depth_per_cell[river_paint]
            masks["hydro_depth"] = d

    # ---- Lakes: force lake ID + depth ----
    if lake_paint.any():
        lk = masks.get("hydro_lake")
        if lk is not None:
            lk = lk.copy() if not lk.flags.writeable else lk
            new_lake_mask = lake_paint & (lk == 0)
            lk[new_lake_mask] = _LAKE_ID_DEFAULT
            masks["hydro_lake"] = lk
        ld = masks.get("hydro_lkdep")
        if ld is not None:
            ld = ld.copy() if not ld.flags.writeable else ld
            np.maximum(ld, _LAKE_DEPTH_MIN, where=lake_paint, out=ld)
            masks["hydro_lkdep"] = ld

    if verbose:
        stats = (
            f"river(spline)={int(river_paint.sum())} "
            f"lake={int(lake_paint.sum())} "
            f"edges={len(_river_edges_cache or [])}"
        )
        print(f"[hydro_region_overlay] applied: {stats}")

    return masks

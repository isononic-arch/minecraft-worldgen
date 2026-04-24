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
_lake_mask_cache: np.ndarray | None = None
_cache_path: Path | None = None


def _build_river_edges(hr_arr: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Skeletonize painted rivers globally and return adjacency-edge list.
    Each edge is (y1, x1, y2, x2) in 8192 coordinates."""
    from skimage.morphology import skeletonize
    river_mask_8k = hr_arr == 2
    if not river_mask_8k.any():
        return []
    skel_8k = skeletonize(river_mask_8k)
    ys, xs = np.where(skel_8k)
    skel_set = set(zip(ys.tolist(), xs.tolist()))
    edges: list[tuple[int, int, int, int]] = []
    # Check 4 of 8 neighbors per pixel to avoid double-counting.
    for dy, dx in ((0, 1), (1, -1), (1, 0), (1, 1)):
        for y, x in zip(ys.tolist(), xs.tolist()):
            ny, nx = y + dy, x + dx
            if (ny, nx) in skel_set:
                edges.append((y, x, ny, nx))
    return edges


def _ensure_caches(hr_path: Path) -> None:
    global _river_edges_cache, _lake_mask_cache, _cache_path
    if _cache_path == hr_path:
        return
    from PIL import Image as _PILImage
    hr_arr = np.asarray(_PILImage.open(hr_path).convert("L"), dtype=np.uint8)
    if hr_arr.shape != (_REGION_PX, _REGION_PX):
        _river_edges_cache = []
        _lake_mask_cache = np.zeros((_REGION_PX, _REGION_PX), dtype=bool)
    else:
        _river_edges_cache = _build_river_edges(hr_arr)
        _lake_mask_cache = (hr_arr == 1)
    _cache_path = hr_path


def _rasterize_river_edges_tile(
    col_off: int, row_off: int, tile_size: int,
) -> np.ndarray:
    """Draw the globally-skeletonized river edges at tile resolution."""
    from skimage.draw import line as draw_line
    out = np.zeros((tile_size, tile_size), dtype=bool)
    if not _river_edges_cache:
        return out
    scale = _WORLD_PX / _REGION_PX  # 6.1
    for y1_8k, x1_8k, y2_8k, x2_8k in _river_edges_cache:
        wy1 = y1_8k * scale; wx1 = x1_8k * scale
        wy2 = y2_8k * scale; wx2 = x2_8k * scale
        ty1 = int(wy1 - row_off); tx1 = int(wx1 - col_off)
        ty2 = int(wy2 - row_off); tx2 = int(wx2 - col_off)
        if (max(ty1, ty2) < -2 or min(ty1, ty2) > tile_size + 1 or
                max(tx1, tx2) < -2 or min(tx1, tx2) > tile_size + 1):
            continue
        rr, cc = draw_line(ty1, tx1, ty2, tx2)
        valid = (rr >= 0) & (rr < tile_size) & (cc >= 0) & (cc < tile_size)
        if valid.any():
            out[rr[valid], cc[valid]] = True
    return out


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
    river_paint = _rasterize_river_edges_tile(col_off, row_off, tile_size)
    lake_paint  = _rasterize_lake_mask_tile(col_off, row_off, tile_size)

    if not (river_paint.any() or lake_paint.any()):
        return masks

    # ---- Rivers: force centerline + minimum order/width/depth ----
    if river_paint.any():
        cl = masks.get("hydro_centerline")
        if cl is not None:
            cl = cl.copy() if not cl.flags.writeable else cl
            cl[river_paint] = np.maximum(cl[river_paint], 1)
            masks["hydro_centerline"] = cl
        ord_ = masks.get("hydro_order")
        if ord_ is not None:
            ord_ = ord_.copy() if not ord_.flags.writeable else ord_
            np.maximum(ord_, _RIVER_ORDER_MIN, where=river_paint, out=ord_)
            masks["hydro_order"] = ord_
        w = masks.get("hydro_width")
        if w is not None:
            w = w.copy() if not w.flags.writeable else w
            np.maximum(w, _RIVER_WIDTH_MIN, where=river_paint, out=w)
            masks["hydro_width"] = w
        d = masks.get("hydro_depth")
        if d is not None:
            d = d.copy() if not d.flags.writeable else d
            np.maximum(d, _RIVER_DEPTH_MIN, where=river_paint, out=d)
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
